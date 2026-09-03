import base64
import hashlib

from cryptography.fernet import Fernet

from .config import get_settings


def _fernet() -> Fernet:
    configured = get_settings().encryption_key.strip()
    if configured:
        try:
            return Fernet(configured.encode())
        except Exception as exc:
            raise RuntimeError("CHAT_ENCRYPTION_KEY must be a valid Fernet key") from exc
    # Backwards-compatible development fallback. Production deployments should
    # set CHAT_ENCRYPTION_KEY so rotating JWT secrets does not invalidate keys.
    key = base64.urlsafe_b64encode(hashlib.sha256(get_settings().jwt_secret.encode()).digest())
    return Fernet(key)


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode() if value else ""


def decrypt_secret(value: str) -> str:
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode()).decode()
    except Exception:
        # Existing development records may contain plaintext keys from the first prototype.
        return value
