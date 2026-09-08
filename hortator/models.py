from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .footer import DEFAULT_FOOTER_TEMPLATE, footer_settings, validate_template

OWNER_ID = "1482143139828596916"
KINDS = ("providers", "profiles", "bots", "prompts", "rooms", "plugins", "settings")
ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$")
SECRET_FIELDS = {
    "api_key",
    "apikey",
    "authorization",
    "token",
    "password",
    "secret",
    "access_token",
    "bot_token",
    "x-api-key",
    "x-subscription-token",
}


class ControlError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def no_secrets(value: Any) -> None:
    if isinstance(value, dict):
        for key, val in value.items():
            if key.lower() in SECRET_FIELDS:
                raise ValueError(
                    f"{key} belongs in the dashboard's credential fields, not configuration JSON"
                )
            no_secrets(val)
    elif isinstance(value, list):
        for val in value:
            no_secrets(val)


class Entity(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    id: str = Field(pattern=ID_PATTERN.pattern)
    name: str = Field(min_length=1, max_length=100)

    @model_validator(mode="before")
    @classmethod
    def secrets_are_separate(cls, value):
        no_secrets(value)
        return value


class Provider(Entity):
    kind: Literal["openrouter", "openai_compatible"] = "openai_compatible"
    base_url: str = "https://openrouter.ai/api/v1"
    enabled: bool = True
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=120, ge=5, le=1800)
    max_concurrency: int = Field(default=4, ge=1, le=100)
    failure_threshold: int = Field(default=3, ge=1, le=30)
    circuit_seconds: float = Field(default=60, ge=1, le=3600)
    requires_key: bool = True
    auth_header: str = Field(default="Authorization", pattern=r"^[A-Za-z0-9-]{1,80}$")
    auth_scheme: str = Field(default="Bearer", max_length=60)

    @field_validator("auth_header")
    @classmethod
    def auth_header_safe(cls, value):
        if value.lower() in {"host", "content-length", "connection", "cookie"}:
            raise ValueError("Cannot use a transport or cookie header for API authentication")
        return value

    @field_validator("base_url")
    @classmethod
    def url(cls, value):
        parsed = urlparse(value)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Use an http(s) base URL without embedded credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("Base URLs cannot contain queries or fragments")
        return value.rstrip("/")

    @field_validator("headers")
    @classmethod
    def safe_headers(cls, value):
        if any(k.lower() in {"host", "cookie", "content-length", "connection"} for k in value):
            raise ValueError("Transport and cookie headers cannot be overridden")
        return value


class Profile(Entity):
    provider_id: str
    model: str = Field(min_length=1, max_length=300)
    context_window: int = Field(default=131072, ge=1024, le=10000000)
    compact_threshold: float = Field(default=0.7, ge=0.1, le=0.95)
    response_tokens: int = Field(default=4096, ge=128, le=1000000)
    summary_tokens: int = Field(default=1024, ge=128, le=32000)
    keep_recent_messages: int = Field(default=12, ge=1, le=1000)
    request_json: dict[str, Any] = Field(default_factory=lambda: {"temperature": 0.8, "max_tokens": 2048})
    compaction_request_json: dict[str, Any] = Field(default_factory=dict)
    stream: bool = True
    include_usage: bool = True
    input_price_per_million: float | None = Field(default=None, ge=0)
    output_price_per_million: float | None = Field(default=None, ge=0)
    cache_hit_input_price_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    cache_miss_input_price_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @field_validator("request_json", "compaction_request_json")
    @classmethod
    def options(cls, value):
        forbidden = {"messages", "model", "tools", "tool_choice", "stream", "n"}.intersection(value)
        if forbidden:
            raise ValueError(
                f"Runtime-owned fields: {', '.join(sorted(forbidden))}; use the stream switch for streaming"
            )
        return value

    @model_validator(mode="after")
    def fits(self):
        if max(self.response_tokens, self.summary_tokens) >= self.context_window * 0.6:
            raise ValueError("Output reserves must be less than 60% of the context window")
        for key in ("max_tokens", "max_completion_tokens"):
            if key in self.request_json:
                n = self.request_json[key]
                if not isinstance(n, int) or isinstance(n, bool) or n < 1 or n > self.response_tokens:
                    raise ValueError(f"{key} must be positive and no larger than response_tokens")
        return self


class Bot(Entity):
    role: Literal["council", "hortator"] = "council"
    application_id: str = ""
    model_profile_id: str
    room_ids: list[str] = Field(default_factory=list, max_length=100)
    enabled: bool = False
    interval_seconds: float = Field(default=60, ge=1, le=86400)
    cooldown_seconds: float = Field(default=60, ge=1, le=86400)
    evaluate_when_idle: bool = True
    prompt_ids: list[str] = Field(default_factory=list, max_length=30)
    persona: str = Field(
        default="Be curious, thoughtful, concise, and willing to disagree constructively.", max_length=60000
    )
    dynamic_prompt: str = Field(default="", max_length=20000)
    enabled_plugins: list[str] = Field(default_factory=list, max_length=50)
    plugin_config: dict[str, dict[str, Any]] = Field(default_factory=dict)
    max_tool_rounds: int = Field(default=3, ge=0, le=20)
    max_calls_per_round: int = Field(default=4, ge=1, le=20)
    document_task_rounds: int = Field(default=20, ge=0, le=100)
    document_task_calls_per_round: int = Field(default=8, ge=1, le=20)
    document_task_seconds: float = Field(default=900, ge=30, le=3600)
    hourly_turn_limit: int = Field(default=120, ge=1, le=10000)
    daily_cost_limit: float | None = Field(default=None, gt=0)
    color: str = Field(default="#b9de89", pattern=r"^#[0-9a-fA-F]{6}$")
    footer_enabled: bool = False
    footer_template: str = DEFAULT_FOOTER_TEMPLATE

    @model_validator(mode="before")
    @classmethod
    def footer_defaults(cls, value):
        if isinstance(value, dict):
            return {**value, **footer_settings(value)}
        return value

    @field_validator("footer_template")
    @classmethod
    def footer_valid(cls, value):
        return validate_template(value)

    @field_validator("application_id")
    @classmethod
    def snowflake(cls, value):
        if value and not re.fullmatch(r"\d{17,20}", value):
            raise ValueError("Application ID must be a Discord snowflake")
        return value


class Prompt(Entity):
    content: str = Field(default="", max_length=100000)


class Room(Entity):
    guild_id: str = ""
    channel_id: str = ""
    include_threads: bool = True
    send_gap_seconds: float = Field(default=1.5, ge=0, le=300)
    allow_humans: bool = True
    allow_external_bots: bool = False

    @field_validator("guild_id", "channel_id")
    @classmethod
    def snowflake(cls, value):
        return Bot.snowflake(value)


class Plugin(Entity):
    enabled: bool = False
    config: dict[str, Any] = Field(default_factory=dict)
    description: str = ""


class Settings(Entity):
    id: Literal["global"] = "global"
    name: str = "Council settings"
    enabled: bool = True
    control_guild_id: str = ""
    control_channel_id: str = ""
    global_prompt: str = "You are part of a council of distinct minds. Engage with others, ask questions, and develop ideas. Let conversations breathe; silence is a valid choice."
    max_concurrent_turns: int = Field(default=8, ge=1, le=100)
    incident_notifications: bool = True
    timezone: str = "Europe/Madrid"

    @field_validator("control_guild_id", "control_channel_id")
    @classmethod
    def snowflake(cls, value):
        return Bot.snowflake(value)

    @field_validator("timezone")
    @classmethod
    def timezone_valid(cls, value):
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(value)
        except (KeyError, ValueError) as exc:
            raise ValueError("Use an IANA timezone such as Europe/Madrid") from exc
        return value


SCHEMAS = {
    "providers": Provider,
    "profiles": Profile,
    "bots": Bot,
    "prompts": Prompt,
    "rooms": Room,
    "plugins": Plugin,
    "settings": Settings,
}
