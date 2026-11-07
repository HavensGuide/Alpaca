"""Encryption of API secrets at rest.

The key comes from the ``ALPACA_MANAGER_KEY`` environment variable if set,
otherwise it is generated once and stored in ``<data_dir>/secret.key`` with
owner-only permissions.
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class SecretBox:
    def __init__(self, data_dir: Path):
        key = os.environ.get("ALPACA_MANAGER_KEY")
        if key:
            self._fernet = Fernet(key.encode())
            return
        key_file = data_dir / "secret.key"
        if key_file.exists():
            key_bytes = key_file.read_bytes().strip()
        else:
            key_bytes = Fernet.generate_key()
            # Create with 0600 so the key is never world-readable, even briefly.
            fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(key_bytes)
        self._fernet = Fernet(key_bytes)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:  # key changed or data corrupted
            raise ValueError(
                "Could not decrypt a stored API secret. Was ALPACA_MANAGER_KEY "
                "or secret.key changed? Re-enter the account's keys."
            ) from exc
