import importlib.metadata
from datetime import datetime
from pathlib import Path
import tomllib

import pytest

from hortator.app import Kernel
from hortator.plugins import ToolContext


STARTER = Path(__file__).resolve().parents[1] / "skills/council-plugin-author/assets/clock-plugin"


@pytest.fixture
async def clock_kernel(tmp_path, monkeypatch):
    metadata = tomllib.loads((STARTER / "pyproject.toml").read_text())
    name, value = next(iter(metadata["project"]["entry-points"]["hortator.plugins"].items()))
    entry = importlib.metadata.EntryPoint(name=name, value=value, group="hortator.plugins")
    monkeypatch.syspath_prepend(str(STARTER / "src"))
    monkeypatch.setattr("hortator.plugins.importlib.metadata.entry_points", lambda **kwargs: [entry])
    k = Kernel(tmp_path)
    async with k.lifetime():
        yield k


def grant_clock(k):
    plugin = k.store.get("plugins", "clock")
    plugin["enabled"] = True
    k.store.put("plugins", plugin)
    bot = k.store.get("bots", "ada")
    bot["enabled_plugins"].append("clock")
    k.store.put("bots", bot)
    return ToolContext(k.store.get("bots", "ada"), "test-channel", "test-turn")


async def test_packaged_clock_help_and_invalid_calls_never_execute(clock_kernel, monkeypatch):
    import council_clock

    def unexpected_clock_read():
        pytest.fail("Discovery or invalid arguments invoked the clock handler")

    monkeypatch.setattr(council_clock, "time", unexpected_clock_read)
    context = grant_clock(clock_kernel)
    help_result = await clock_kernel.registry.call_raw("clock", "{}", context, "clock-help")
    assert help_result["usage_only"] and not help_result["executed"]
    assert help_result["usage"]["example"] == {"operation": "now"}
    for index, arguments in enumerate(({"operation": "invalid"}, {"unexpected": True})):
        result = await clock_kernel.registry.call("clock", arguments, context, f"clock-invalid-{index}")
        assert not result["ok"] and result["errors"]


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        ("2026-01-15T12:00:00+00:00", "2026-01-15T13:00:00+01:00"),
        ("2026-07-15T12:00:00+00:00", "2026-07-15T14:00:00+02:00"),
    ],
)
async def test_packaged_clock_action_uses_council_timezone(clock_kernel, monkeypatch, instant, expected):
    import council_clock

    monkeypatch.setattr(council_clock, "time", lambda: datetime.fromisoformat(instant).timestamp())
    settings = clock_kernel.store.get("settings", "global")
    settings["timezone"] = "Europe/Madrid"
    clock_kernel.store.put("settings", settings)
    context = grant_clock(clock_kernel)
    result = await clock_kernel.registry.call_raw("clock", '{"operation":"now"}', context, "clock-execution")
    assert result["now"] == expected
    assert result["result_id"]
    assert clock_kernel.vault.get("plugin/clock/api_key") == ""


async def test_packaged_clock_defaults_and_grant_revocation(clock_kernel, monkeypatch):
    import council_clock

    def unexpected_clock_read():
        pytest.fail("An ungranted call invoked the clock handler")

    monkeypatch.setattr(council_clock, "time", unexpected_clock_read)
    plugin = clock_kernel.store.get("plugins", "clock")
    assert not plugin["enabled"]
    assert clock_kernel.service.public("plugins", plugin)["keyless"]
    bot = clock_kernel.store.get("bots", "ada")
    assert "clock" not in bot["enabled_plugins"]
    context = ToolContext(bot, "test-channel", "test-turn")
    assert not clock_kernel.registry.allowed("clock", context)
    context = grant_clock(clock_kernel)
    advertised = clock_kernel.registry.schemas(context)
    assert any(tool["function"]["name"] == "clock" for tool in advertised)
    current_bot = clock_kernel.store.get("bots", "ada")
    current_bot["enabled_plugins"].remove("clock")
    clock_kernel.store.put("bots", current_bot)
    # Execution must recheck the current grant even with a captured granted bot.
    result = await clock_kernel.registry.call("clock", {"operation": "now"}, context, "clock-revoked")
    assert not result["ok"] and "not enabled" in result["error"]
