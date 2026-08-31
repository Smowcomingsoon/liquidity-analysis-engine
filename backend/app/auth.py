"""
Minimal cookie-based auth for a small multi-user personal tool.

Password hashing: stdlib PBKDF2-HMAC (no new dependency). Sessions: a
signed cookie (userid:expiry:HMAC-signature) rather than a server-side
session table — stateless, so logins survive a server restart/redeploy
without needing extra persistence beyond the signing secret itself.

No email verification (deliberately skipped for now — registration is
just email+password). Each user pastes in their own Telegram chat_id
(grabbed from a DM with the shared bot) so watchlist alerts reach them
individually instead of a single shared broadcast list.
"""

import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path

from fastapi import Cookie, HTTPException

from app.models import User
from app.trading.crypto import encrypt as _enc, decrypt as _dec

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_USERS_FILE = _DATA_DIR / "users.json"
_SECRET_FILE = _DATA_DIR / "session_secret.txt"

SESSION_COOKIE = "session"
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 30  # 30 days

_PBKDF2_ITERATIONS = 200_000

_users: dict[str, User] = {}


def _get_secret() -> bytes:
    if _SECRET_FILE.exists():
        return _SECRET_FILE.read_text(encoding="utf-8").strip().encode("utf-8")
    secret = secrets.token_hex(32)
    _SECRET_FILE.write_text(secret, encoding="utf-8")
    return secret.encode("utf-8")


_SECRET = _get_secret()


def _load_users() -> None:
    if not _USERS_FILE.exists():
        return
    try:
        raw = json.loads(_USERS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    for item in raw:
        try:
            user = User(**item)
            _users[user.id] = user
        except (TypeError, ValueError):
            continue


def _save_users() -> None:
    payload = [u.model_dump() for u in _users.values()]
    _USERS_FILE.write_text(json.dumps(payload), encoding="utf-8")


_load_users()


def _hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"{salt.hex()}${dk.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, _ = stored.split("$", 1)
    except ValueError:
        return False
    salt = bytes.fromhex(salt_hex)
    return hmac.compare_digest(_hash_password(password, salt), stored)


# A short blocklist of the passwords most likely to appear in a leaked
# credential-stuffing list — not a substitute for a real breach-check API,
# just cheap insurance against the most obvious choices.
_COMMON_WEAK_PASSWORDS = {
    "password", "password1", "password12", "password123",
    "12345678", "123456789", "1234567890",
    "qwerty123", "qwertyui", "qwertyuiop",
    "letmein1", "letmein123", "admin1234", "iloveyou1", "welcome1",
}


def is_strong_password(password: str) -> bool:
    if len(password) < 8:
        return False
    if password.lower() in _COMMON_WEAK_PASSWORDS:
        return False
    has_letter = any(c.isalpha() for c in password)
    has_digit = any(c.isdigit() for c in password)
    return has_letter and has_digit


def get_user(user_id: str) -> User | None:
    return _users.get(user_id)


def find_by_email(email: str) -> User | None:
    email = email.strip().lower()
    return next((u for u in _users.values() if u.email == email), None)


def create_user(email: str, password: str) -> User:
    user = User(
        id=secrets.token_hex(8),
        email=email.strip().lower(),
        password_hash=_hash_password(password),
        telegram_chat_id="",
    )
    _users[user.id] = user
    _save_users()
    return user


def verify_login(email: str, password: str) -> User | None:
    user = find_by_email(email)
    if user is None or not _verify_password(password, user.password_hash):
        return None
    return user


def set_telegram_chat_id(user_id: str, chat_id: str) -> User | None:
    user = _users.get(user_id)
    if user is None:
        return None
    updated = user.model_copy(update={"telegram_chat_id": chat_id.strip()})
    _users[user_id] = updated
    _save_users()
    return updated


def set_trading_credential(
    user_id: str,
    exchange: str,
    api_key: str,
    api_secret: str,
    passphrase: str,
    enabled: bool,
    size_mode: str,
    size_value: float,
) -> User | None:
    from app.models import ExchangeCredential
    user = _users.get(user_id)
    if user is None:
        return None
    creds = dict(user.trading_credentials)
    creds[exchange] = ExchangeCredential(
        api_key_enc=_enc(api_key, _SECRET) if api_key else creds.get(exchange, ExchangeCredential()).api_key_enc,
        api_secret_enc=_enc(api_secret, _SECRET) if api_secret else creds.get(exchange, ExchangeCredential()).api_secret_enc,
        passphrase_enc=_enc(passphrase, _SECRET) if passphrase else creds.get(exchange, ExchangeCredential()).passphrase_enc,
        enabled=enabled,
        size_mode=size_mode,
        size_value=size_value,
    )
    updated = user.model_copy(update={"trading_credentials": creds})
    _users[user_id] = updated
    _save_users()
    return updated


def get_decrypted_credential(user: User, exchange: str) -> tuple[str, str, str]:
    """Returns (api_key, api_secret, passphrase) decrypted. Empty strings if not configured."""
    cred = user.trading_credentials.get(exchange)
    if cred is None:
        return "", "", ""
    return _dec(cred.api_key_enc, _SECRET), _dec(cred.api_secret_enc, _SECRET), _dec(cred.passphrase_enc, _SECRET)


def make_session_cookie(user_id: str) -> str:
    expires = int(time.time()) + SESSION_MAX_AGE_SECONDS
    payload = f"{user_id}:{expires}"
    sig = hmac.new(_SECRET, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def _verify_session_cookie(cookie: str) -> str | None:
    try:
        user_id, expires_s, sig = cookie.split(":", 2)
    except ValueError:
        return None
    expected = hmac.new(_SECRET, f"{user_id}:{expires_s}".encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    if int(expires_s) < time.time():
        return None
    return user_id


async def get_current_user_optional(session: str | None = Cookie(default=None, alias=SESSION_COOKIE)) -> User | None:
    user_id = _verify_session_cookie(session) if session else None
    return _users.get(user_id) if user_id else None


async def get_current_user(session: str | None = Cookie(default=None, alias=SESSION_COOKIE)) -> User:
    user = await get_current_user_optional(session)
    if user is None:
        raise HTTPException(status_code=401, detail="Accesso richiesto")
    return user
