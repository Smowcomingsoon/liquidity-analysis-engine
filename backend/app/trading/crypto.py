import base64
import hashlib
import hmac
import os


def encrypt(plaintext: str, secret: bytes) -> str:
    """Encrypt a string with secret, returns base64-encoded ciphertext (16-byte nonce + ciphertext)."""
    data = plaintext.encode()
    nonce = os.urandom(16)
    stream = _keystream(secret, nonce, len(data))
    return base64.b64encode(nonce + bytes(a ^ b for a, b in zip(data, stream))).decode()


def decrypt(ciphertext_b64: str, secret: bytes) -> str:
    """Decrypt. Returns empty string if ciphertext is empty or malformed."""
    if not ciphertext_b64:
        return ""
    try:
        raw = base64.b64decode(ciphertext_b64)
        nonce, data = raw[:16], raw[16:]
        stream = _keystream(secret, nonce, len(data))
        return bytes(a ^ b for a, b in zip(data, stream)).decode()
    except Exception:  # noqa: BLE001
        return ""


def _keystream(secret: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(secret, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])
