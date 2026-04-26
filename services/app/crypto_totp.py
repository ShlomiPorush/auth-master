import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import get_settings


def _aes_key() -> bytes:
    s = get_settings()
    return hashlib.sha256((s.session_secret + s.app_encryption_key).encode("utf-8")).digest()


def encrypt_totp_secret(plaintext: str) -> str:
    key = _aes_key()
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    ct = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ct).decode("ascii")


def decrypt_totp_secret(blob_b64: str) -> str:
    key = _aes_key()
    raw = base64.b64decode(blob_b64)
    nonce, ct = raw[:12], raw[12:]
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ct, None).decode("utf-8")
