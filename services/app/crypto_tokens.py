import hashlib
import os
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import get_settings


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def generate_raw_token() -> str:
    return secrets.token_urlsafe(32)


def _aes_key() -> bytes:
    s = get_settings()
    return hashlib.sha256((s.session_secret + s.app_encryption_key).encode("utf-8")).digest()


def encrypt_token_value(raw_token: str) -> str:
    key = _aes_key()
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    ct = aesgcm.encrypt(nonce, raw_token.encode("utf-8"), None)
    return (nonce + ct).hex()


def decrypt_token_value(enc_hex: str) -> str:
    key = _aes_key()
    raw = bytes.fromhex(enc_hex)
    nonce, ct = raw[:12], raw[12:]
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ct, None).decode("utf-8")
