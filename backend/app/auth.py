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

from app.models import OnchainChain, OnchainWallet, Passkey, User
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
    # Env-var wins: always available even before the volume mounts, so a
    # rolling deploy never regenerates the secret and logs everyone out.
    import os
    env_secret = os.environ.get("SESSION_SECRET", "").strip()
    if env_secret:
        return env_secret.encode("utf-8")
    # Fallback: persistent file on the Fly volume (works for local dev and
    # as a one-time seed before the env-var is set).
    if _SECRET_FILE.exists():
        secret = _SECRET_FILE.read_text(encoding="utf-8").strip()
        if secret:
            return secret.encode("utf-8")
    secret = secrets.token_hex(32)
    _SECRET_FILE.write_text(secret, encoding="utf-8")
    return secret.encode("utf-8")


_SECRET = _get_secret()
# Segreto derivato, non riusato: _SECRET firma anche i cookie di sessione, e
# una chiave che sposta fondi veri non deve dipendere dallo stesso materiale
# di una che autentica un browser. HMAC come PRF con un'etichetta fissa —
# stesso principio di HKDF, senza bisogno di una libreria in piu'.
_ONCHAIN_SECRET = hmac.new(_SECRET, b"onchain-wallet-v1", hashlib.sha256).digest()


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


def list_users() -> list[User]:
    return list(_users.values())


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


def set_sniper_autotrade_chain(user_id: str, chain: str, enabled: bool) -> User | None:
    user = _users.get(user_id)
    if user is None:
        return None
    chains = set(user.sniper_autotrade_chains)
    if enabled:
        chains.add(chain)
    else:
        chains.discard(chain)
    updated = user.model_copy(update={"sniper_autotrade_chains": sorted(chains)})
    _users[user_id] = updated
    _save_users()
    return updated


def set_preferences(user_id: str, prefs: dict) -> User | None:
    user = _users.get(user_id)
    if user is None:
        return None
    updated = user.model_copy(update={"preferences": prefs})
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
    existing = creds.get(exchange, ExchangeCredential())
    new_key = _enc(api_key, _SECRET) if api_key else existing.api_key_enc
    creds[exchange] = ExchangeCredential(
        api_key_enc=new_key,
        api_secret_enc=_enc(api_secret, _SECRET) if api_secret else existing.api_secret_enc,
        passphrase_enc=_enc(passphrase, _SECRET) if passphrase else existing.passphrase_enc,
        enabled=enabled,
        size_mode=size_mode,
        size_value=size_value,
        # Impostata solo la prima volta che la chiave smette di essere vuota —
        # una modifica successiva (leva, size, toggle) non deve azzerare la
        # data di registrazione mostrata all'utente.
        created_at=existing.created_at or (time.time() if new_key else 0.0),
    )
    updated = user.model_copy(update={"trading_credentials": creds})
    _users[user_id] = updated
    _save_users()
    return updated


def delete_trading_credential(user_id: str, exchange: str) -> User | None:
    user = _users.get(user_id)
    if user is None:
        return None
    creds = dict(user.trading_credentials)
    creds.pop(exchange, None)
    updated = user.model_copy(update={"trading_credentials": creds})
    _users[user_id] = updated
    _save_users()
    return updated


def delete_futures_credential(user_id: str, exchange: str) -> User | None:
    user = _users.get(user_id)
    if user is None:
        return None
    creds = dict(user.futures_credentials)
    creds.pop(exchange, None)
    updated = user.model_copy(update={"futures_credentials": creds})
    _users[user_id] = updated
    _save_users()
    return updated


def get_decrypted_credential(user: User, exchange: str) -> tuple[str, str, str]:
    """Returns (api_key, api_secret, passphrase) decrypted. Empty strings if not configured."""
    cred = user.trading_credentials.get(exchange)
    if cred is None:
        return "", "", ""
    return _dec(cred.api_key_enc, _SECRET), _dec(cred.api_secret_enc, _SECRET), _dec(cred.passphrase_enc, _SECRET)


def set_futures_credential(
    user_id: str,
    exchange: str,
    api_key: str,
    api_secret: str,
    enabled: bool,
    size_mode: str,
    size_value: float,
    leverage: int,
    passphrase: str = "",
) -> User | None:
    """Modello e file separati da set_trading_credential() apposta — vedi
    FuturesCredential nel piano "Operazioni futures": un bug qui non deve
    poter toccare le credenziali spot, e viceversa."""
    from app.models import MAX_FUTURES_LEVERAGE, FuturesCredential
    if not (1 <= leverage <= MAX_FUTURES_LEVERAGE):
        raise ValueError(f"Leverage {leverage}x outside the allowed range (1-{MAX_FUTURES_LEVERAGE}x)")
    user = _users.get(user_id)
    if user is None:
        return None
    creds = dict(user.futures_credentials)
    existing = creds.get(exchange, FuturesCredential())
    new_key = _enc(api_key, _SECRET) if api_key else existing.api_key_enc
    creds[exchange] = FuturesCredential(
        api_key_enc=new_key,
        api_secret_enc=_enc(api_secret, _SECRET) if api_secret else existing.api_secret_enc,
        # Vuota = non cambiare, come per chiave e secret: chi salva solo un
        # cambio di leva non deve riscrivere tutte le credenziali.
        passphrase_enc=_enc(passphrase, _SECRET) if passphrase else existing.passphrase_enc,
        enabled=enabled,
        size_mode=size_mode,
        size_value=size_value,
        leverage=leverage,
        created_at=existing.created_at or (time.time() if new_key else 0.0),
    )
    updated = user.model_copy(update={"futures_credentials": creds})
    _users[user_id] = updated
    _save_users()
    return updated


def get_decrypted_futures_credential(user: User, exchange: str) -> tuple[str, str, str]:
    """Returns (api_key, api_secret, passphrase) decrypted. Empty strings if
    not configured. La passphrase resta vuota dove l'exchange non la chiede
    (Binance, Bybit, Kraken) ed e' obbligatoria dove la chiede (OKX,
    Bitget) — a saperlo e' l'adattatore, non questo modulo."""
    cred = user.futures_credentials.get(exchange)
    if cred is None:
        return "", "", ""
    return (
        _dec(cred.api_key_enc, _SECRET),
        _dec(cred.api_secret_enc, _SECRET),
        _dec(cred.passphrase_enc, _SECRET),
    )


def create_onchain_wallet(user_id: str, chain: OnchainChain) -> User | None:
    """Genera una chiave nuova per quella chain — mai due volte: una
    seconda chiamata su una chain che ha gia' un wallet lo lascia intatto
    (rigenerare in silenzio orfanerebbe qualunque fondo gia' mandato al
    vecchio indirizzo). Nasce spenta (enabled=False): un kill switch
    esplicito va acceso a parte, non e' un effetto collaterale del crearla."""
    from app.trading.onchain.keys import generate  # import qui: onchain.keys non deve mai importare auth

    user = _users.get(user_id)
    if user is None:
        return None
    if chain in user.onchain_wallets:
        return user
    key = generate(chain)
    wallets = dict(user.onchain_wallets)
    wallets[chain] = OnchainWallet(
        address=key.address,
        private_key_enc=_enc(key.private_key_hex, _ONCHAIN_SECRET),
        enabled=False,
        created_at=time.time(),
    )
    updated = user.model_copy(update={"onchain_wallets": wallets})
    _users[user_id] = updated
    _save_users()
    return updated


def set_onchain_wallet_enabled(user_id: str, chain: OnchainChain, enabled: bool) -> User | None:
    user = _users.get(user_id)
    if user is None or chain not in user.onchain_wallets:
        return None
    wallets = dict(user.onchain_wallets)
    wallets[chain] = wallets[chain].model_copy(update={"enabled": enabled})
    updated = user.model_copy(update={"onchain_wallets": wallets})
    _users[user_id] = updated
    _save_users()
    return updated


class OnchainKeyIntegrityError(Exception):
    """La chiave decifrata non ricava piu' l'indirizzo salvato — non e'
    un errore silenzioso da ignorare: il cifrario usato altrove in questo
    file basta per un'API key (l'exchange la rifiuta se sbagliata), ma qui
    una corruzione andrebbe rilevata prima di firmare qualunque cosa con
    lei, non dopo aver perso l'accesso al wallet."""


def get_decrypted_onchain_private_key(user: User, chain: OnchainChain) -> str:
    from app.trading.onchain.keys import address_from_private_key

    wallet = user.onchain_wallets.get(chain)
    if wallet is None:
        return ""
    private_key_hex = _dec(wallet.private_key_enc, _ONCHAIN_SECRET)
    if not private_key_hex:
        return ""
    if address_from_private_key(chain, private_key_hex) != wallet.address:
        raise OnchainKeyIntegrityError(
            f"The decrypted key for {chain} does not match the saved address "
            f"({wallet.address}) — corrupted data, not used."
        )
    return private_key_hex


def add_passkey(user_id: str, passkey: Passkey) -> User | None:
    user = _users.get(user_id)
    if user is None:
        return None
    updated = user.model_copy(update={"passkeys": [*user.passkeys, passkey]})
    _users[user_id] = updated
    _save_users()
    return updated


def delete_passkey(user_id: str, credential_id: str) -> User | None:
    user = _users.get(user_id)
    if user is None:
        return None
    updated = user.model_copy(update={"passkeys": [pk for pk in user.passkeys if pk.credential_id != credential_id]})
    _users[user_id] = updated
    _save_users()
    return updated


def update_passkey_sign_count(user_id: str, credential_id: str, new_sign_count: int) -> User | None:
    """Aggiornato dopo OGNI autenticazione riuscita — un sign_count che
    smette di crescere rispetto a quello salvato e' come WebAuthn segnala
    un passkey clonato, quindi va scritto subito, non in un giro comodo."""
    user = _users.get(user_id)
    if user is None:
        return None
    passkeys = [
        pk.model_copy(update={"sign_count": new_sign_count}) if pk.credential_id == credential_id else pk
        for pk in user.passkeys
    ]
    updated = user.model_copy(update={"passkeys": passkeys})
    _users[user_id] = updated
    _save_users()
    return updated


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


async def get_current_user_optional(
    session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    guest: str | None = Cookie(default=None),
) -> User | None:
    user_id = _verify_session_cookie(session) if session else None
    if user_id and user_id in _users:
        return _users[user_id]
    # Link ospite (app/guest.py): utente sintetico in sola lettura. Una
    # sessione vera vince sempre, cosi' Lorenzo non diventa ospite a casa sua.
    from app import guest as guest_links
    link = guest_links.verify_cookie(guest)
    return guest_links.guest_user(link) if link else None


async def get_current_user(
    session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    guest: str | None = Cookie(default=None),
) -> User:
    user = await get_current_user_optional(session, guest)
    if user is None:
        raise HTTPException(status_code=401, detail="Sign-in required")
    return user
