"""Local operator bootstrap for SSH identities; no model or network entry point."""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .models import ControlError
from .security import Vault


@dataclass(frozen=True)
class PublicIdentity:
    public_key: str
    fingerprint: str
    created: bool


def ssh_identity(vault: Vault, name: str, *, create: bool = False) -> PublicIdentity:
    """Generate once, encrypt in the existing vault, and return only public data.

    The caller must hold the runtime lock: a running Vault caches its secrets.
    Repeating create never replaces an existing identity, including a damaged one.
    """
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", name):
        raise ControlError(
            "SSH identity name must be 1–48 lowercase letters, digits, '_' or '-', starting with a letter"
        )
    scope = f"ssh/{name}/private_key"
    stored = vault.get(scope)
    created = False
    if stored:
        try:
            key = serialization.load_ssh_private_key(stored.encode(), password=None)
        except ValueError, TypeError:
            raise ControlError("Stored SSH identity is invalid; it was not replaced") from None
        if not isinstance(key, Ed25519PrivateKey):
            raise ControlError("Stored SSH identity is not Ed25519; it was not replaced")
    else:
        if not create:
            raise ControlError("SSH identity does not exist; use 'ssh-key create' to generate it")
        key = Ed25519PrivateKey.generate()
        # The OpenSSH representation exists only in memory. Fernet protects the
        # persisted value; unattended transport will not need a separate password.
        private = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        ).decode()
        vault.put(scope, private)
        created = True
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )
    digest = hashlib.sha256(base64.b64decode(public.split()[1])).digest()
    fingerprint = "SHA256:" + base64.b64encode(digest).decode().rstrip("=")
    if created:
        vault.store.emit("ssh.identity_created", {"name": name, "fingerprint": fingerprint})
    return PublicIdentity(f"{public} hortator-{name}", fingerprint, created)
