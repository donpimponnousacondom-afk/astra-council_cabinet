from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet

from .models import ControlError, OWNER_ID, SECRET_FIELDS


@dataclass(frozen=True)
class Actor:
    source: str
    user_id: str
    is_bot: bool = False
    webhook_id: str | None = None

    def require_owner(self):
        if (
            self.source not in ("dashboard", "discord", "local")
            or self.user_id != OWNER_ID
            or self.is_bot
            or self.webhook_id
        ):
            raise ControlError("Only The Boss can control the council", 403)

    @property
    def label(self):
        return f"{self.source}:{self.user_id}"


class Vault:
    def __init__(self, store, directory: Path):
        self.store = store
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
        path = directory / "master.key"
        key = os.environ.get("HORTATOR_MASTER_KEY")
        if not key:
            if not path.exists():
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(Fernet.generate_key())
            key = path.read_text().strip()
        self.fernet = Fernet(key.encode())
        # Fail closed if an existing encrypted database uses a different key.
        self.values = {
            r["scope"]: self.fernet.decrypt(r["value"]).decode() for r in store.rows("SELECT * FROM secrets")
        }
        store.redact = self.redact

    def get(self, scope):
        return self.values.get(scope, "")

    def put(self, scope, value):
        if value:
            self.store.execute(
                "INSERT INTO secrets(scope,value) VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET value=excluded.value",
                (scope, self.fernet.encrypt(value.encode())),
            )
            self.values[scope] = value
        else:
            self.store.execute("DELETE FROM secrets WHERE scope=?", (scope,))
            self.values.pop(scope, None)

    def redact(self, value: Any):
        if isinstance(value, dict):
            return {
                k: ("[REDACTED]" if k.lower() in SECRET_FIELDS else self.redact(v)) for k, v in value.items()
            }
        if isinstance(value, list):
            return [self.redact(x) for x in value]
        if isinstance(value, str):
            # Verified Discord user IDs are public identity metadata, even though
            # stored beside the token. Redacting them would corrupt application
            # IDs, OAuth invitations, and speaker attribution in the dashboard.
            secrets_to_hide = (v for scope, v in self.values.items() if not scope.endswith("/user_id"))
            for secret in sorted(secrets_to_hide, key=len, reverse=True):
                if len(secret) >= 6:
                    value = value.replace(secret, "[REDACTED]")
            value = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+=*", r"\1[REDACTED]", value)
        return value


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    result = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return f"{salt}:{result}"


class Auth:
    def __init__(self, store, vault, directory):
        self.store, self.vault = store, vault
        self.failures: dict[str, list[float]] = {}
        supplied = os.getenv("HORTATOR_ADMIN_PASSWORD")
        if supplied:
            if len(supplied) < 12:
                raise RuntimeError("HORTATOR_ADMIN_PASSWORD must have at least 12 characters")
            encoded = vault.get("auth/password")
            if not encoded or not hmac.compare_digest(
                encoded, password_hash(supplied, encoded.split(":")[0])
            ):
                vault.put("auth/password", password_hash(supplied))
                store.execute("DELETE FROM auth_sessions")
        elif not vault.get("auth/password"):
            password = secrets.token_urlsafe(24)
            path = directory / "initial-password"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as file:
                file.write(password + "\n")
            vault.put("auth/password", password_hash(password))

    def login(self, password, peer):
        now = time.time()
        self.failures = {
            key: [t for t in times if t > now - 300]
            for key, times in self.failures.items()
            if any(t > now - 300 for t in times)
        }
        attempts = self.failures.setdefault(peer, [])
        if len(attempts) >= 8 or sum(map(len, self.failures.values())) > 200:
            raise ControlError("Too many login attempts. Try again in five minutes.", 429)
        encoded = self.vault.get("auth/password")
        candidate = password_hash(password, encoded.split(":")[0]) if len(password) <= 1024 else ""
        if not hmac.compare_digest(encoded, candidate):
            attempts.append(now)
            raise ControlError("Incorrect password", 401)
        self.failures.pop(peer, None)
        token, csrf = secrets.token_urlsafe(40), secrets.token_urlsafe(32)
        self.store.execute("DELETE FROM auth_sessions WHERE expires_at<?", (now,))
        self.store.execute(
            "INSERT INTO auth_sessions VALUES(?,?,?)",
            (hashlib.sha256(token.encode()).hexdigest(), csrf, now + 43200),
        )
        return token, csrf

    def session(self, token):
        if not token:
            raise ControlError("Sign in to continue", 401)
        session = self.store.one(
            "SELECT * FROM auth_sessions WHERE token_hash=? AND expires_at>?",
            (hashlib.sha256(token.encode()).hexdigest(), time.time()),
        )
        if not session:
            raise ControlError("Session expired. Sign in again.", 401)
        return session

    def logout(self, token):
        self.store.execute(
            "DELETE FROM auth_sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),)
        )
