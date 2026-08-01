from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken


class CredentialError(RuntimeError):
    pass


class CredentialVault:
    """Encrypt account passwords before they are written to SQLite."""

    def __init__(self, master_key: str) -> None:
        material = master_key.strip()
        if len(material) < 16:
            raise CredentialError(
                "SD2API_CREDENTIAL_KEY or SD2API_ADMIN_KEY must contain at least 16 characters"
            )
        digest = hashlib.sha256(("sd2api-credentials-v1\0" + material).encode("utf-8")).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError) as exc:
            raise CredentialError(
                "Stored account credentials cannot be decrypted; check SD2API_CREDENTIAL_KEY"
            ) from exc
