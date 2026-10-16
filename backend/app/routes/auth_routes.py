"""Sign-up, sign-in, sessions, passkeys and account settings.

Moved out of app/main.py (overhaul plan, phase 5); same paths, same code.
"""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from app import auth, rate_limit
from app import guest as guest_links
from app.config import settings
from app.models import (
    MAX_FUTURES_LEVERAGE,
    User,
)
from app.notify import telegram
from app.trading.futures import registry as futures_registry
from app.trading.onchain import passkey as onchain_passkey
from app.web_common import (
    SUPPORTED_FUTURES_EXCHANGES_ORDERED,
    _client_ip,
    _fire,
    _is_https,
    _telegram_allowed,
    _user_out,
)

router = APIRouter()


class TradingCredentialRequest(BaseModel):
    exchange: str
    api_key: str = ""        # empty = keep existing
    api_secret: str = ""     # empty = keep existing
    passphrase: str = ""     # empty = keep existing
    enabled: bool = False
    size_mode: Literal["fixed_usdt", "pct_balance"] = "fixed_usdt"
    size_value: float = 50.0


class TradingCredentialOut(BaseModel):
    exchange: str
    api_key_set: bool        # True if an encrypted key exists
    api_secret_set: bool
    passphrase_set: bool
    enabled: bool
    size_mode: str
    size_value: float
    created_at: float = 0.0  # epoch seconds; 0 = mai configurata


class FuturesCredentialRequest(BaseModel):
    exchange: str
    api_key: str = ""     # empty = keep existing
    api_secret: str = ""  # empty = keep existing
    passphrase: str = ""  # empty = keep existing — serve su OKX e Bitget
    enabled: bool = False  # kill switch — spegnibile senza toccare le chiavi
    size_mode: Literal["fixed_usdt", "pct_balance"] = "fixed_usdt"
    size_value: float = 50.0
    leverage: int = 1


class FuturesCredentialOut(BaseModel):
    exchange: str
    name: str
    api_key_set: bool
    api_secret_set: bool
    passphrase_set: bool
    needs_passphrase: bool
    # Il tetto e' del singolo exchange, non piu' uno solo per tutti: la
    # schermata deve poter fermare il cursore dove lo ferma il server.
    max_leverage: int
    isolated_margin: bool
    key_hint: str
    enabled: bool
    size_mode: str
    size_value: float
    leverage: int
    created_at: float = 0.0  # epoch seconds; 0 = mai configurata


class RegisterRequest(BaseModel):
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


class TelegramChatIdRequest(BaseModel):
    telegram_chat_id: str
    telegram_signal_chat_id: str | None = None  # None = leave as is


def _set_session_cookie(response: Response, user_id: str, request: Request) -> None:
    response.set_cookie(
        auth.SESSION_COOKIE,
        auth.make_session_cookie(user_id),
        max_age=auth.SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=_is_https(request),
    )


@router.post("/api/auth/register")


async def register(req: RegisterRequest, request: Request, response: Response) -> dict:
    ip_key = f"register:{_client_ip(request)}"
    if rate_limit.is_locked(ip_key):
        raise HTTPException(status_code=429, detail="Too many attempts. Try again in a few minutes.")

    email = req.email.strip().lower()
    if "@" not in email or len(email) < 5:
        rate_limit.register_failure(ip_key)
        raise HTTPException(status_code=400, detail="Invalid email")

    consentite = {e.strip().lower() for e in settings.registration_allowed_emails.split(",") if e.strip()}
    if consentite and email not in consentite:
        rate_limit.register_failure(ip_key)
        # Stesso messaggio che si da' a un'email gia' presente: due risposte
        # diverse direbbero a un estraneo quali indirizzi esistono.
        raise HTTPException(status_code=403, detail="Registration not allowed for this address")
    if not auth.is_strong_password(req.password):
        rate_limit.register_failure(ip_key)
        raise HTTPException(
            status_code=400,
            detail="Password must be at least 8 characters, with letters and numbers, and not too common",
        )
    if auth.find_by_email(email):
        rate_limit.register_failure(ip_key)
        raise HTTPException(status_code=400, detail="Email already registered")

    user = auth.create_user(email, req.password)
    rate_limit.register_success(ip_key)
    _set_session_cookie(response, user.id, request)
    return _user_out(user)


@router.post("/api/auth/login")


async def login(req: LoginRequest, request: Request, response: Response) -> dict:
    email = req.email.strip().lower()
    ip_key = f"ip:{_client_ip(request)}"
    email_key = f"email:{email}"
    if rate_limit.is_locked(ip_key) or rate_limit.is_locked(email_key):
        raise HTTPException(status_code=429, detail="Too many failed attempts. Try again in a few minutes.")

    user = auth.verify_login(email, req.password)
    if user is None:
        rate_limit.register_failure(ip_key)
        rate_limit.register_failure(email_key)
        raise HTTPException(status_code=401, detail="Email o password errati")

    rate_limit.register_success(ip_key)
    rate_limit.register_success(email_key)
    _set_session_cookie(response, user.id, request)
    return _user_out(user)


@router.post("/api/auth/logout")


async def logout(request: Request, response: Response) -> dict:
    auth.revoke_session(request.cookies.get(auth.SESSION_COOKIE) or "")
    response.delete_cookie(auth.SESSION_COOKIE)
    response.delete_cookie(guest_links.GUEST_COOKIE)
    return {"ok": True}


@router.post("/api/auth/logout-all")


async def logout_all(response: Response, user: User = Depends(auth.get_current_user)) -> dict:
    """Every session of this account, on every device, stops working."""
    auth.revoke_all_sessions(user.id)
    response.delete_cookie(auth.SESSION_COOKIE)
    return {"ok": True}


@router.get("/api/auth/me")


async def me(user: User | None = Depends(auth.get_current_user_optional)) -> dict:
    return {"user": _user_out(user) if user else None}


@router.patch("/api/auth/telegram")


async def update_telegram_chat_id(
    req: TelegramChatIdRequest, user: User = Depends(auth.get_current_user)
) -> dict:
    before = user.telegram_signal_chat_id
    updated = auth.set_telegram_chat_id(user.id, req.telegram_chat_id, req.telegram_signal_chat_id)
    if (updated.telegram_signal_chat_id and updated.telegram_signal_chat_id != before
            and _telegram_allowed(updated)):
        _fire(telegram.send_to(
            updated.telegram_signal_chat_id,
            "✅ Chat degli alert collegata: da qui in poi qui arrivano solo i segnali delle strategie (e gli alert di prezzo).",
        ))
    if updated.telegram_chat_id and _telegram_allowed(updated):
        # Confirms the chat_id was actually saved and is reachable — sent
        # immediately on save, not the first real alert, so a typo'd id
        # shows up right away instead of silently going nowhere.
        _fire(
            telegram.send_to(
                updated.telegram_chat_id,
                "✅ Chat ID collegato correttamente a Liquidity Analysis Engine.\n"
                "Da qui in poi riceverai qui gli avvisi delle strategie che attivi.",
            )
        )
    return _user_out(updated)


@router.get("/api/auth/trading", response_model=list[TradingCredentialOut])


async def get_trading_credentials(user: User = Depends(auth.get_current_user)) -> list[TradingCredentialOut]:
    out = []
    for exchange in ("binance", "mexc", "kucoin", "kraken", "coinbase", "bitstamp"):
        cred = user.trading_credentials.get(exchange)
        if cred is None:
            out.append(TradingCredentialOut(exchange=exchange, api_key_set=False, api_secret_set=False, passphrase_set=False, enabled=False, size_mode="fixed_usdt", size_value=50.0))
        else:
            out.append(TradingCredentialOut(
                exchange=exchange,
                api_key_set=bool(cred.api_key_enc),
                api_secret_set=bool(cred.api_secret_enc),
                passphrase_set=bool(cred.passphrase_enc),
                enabled=cred.enabled,
                size_mode=cred.size_mode,
                size_value=cred.size_value,
                created_at=cred.created_at,
            ))
    return out


@router.patch("/api/auth/trading")


async def set_trading_credential(body: TradingCredentialRequest, user: User = Depends(auth.get_current_user)):
    supported = {"binance", "mexc", "kucoin", "kraken", "coinbase", "bitstamp"}
    if body.exchange not in supported:
        raise HTTPException(status_code=400, detail=f"Unsupported exchange: {body.exchange}")
    auth.set_trading_credential(
        user.id, body.exchange, body.api_key, body.api_secret, body.passphrase,
        body.enabled, body.size_mode, body.size_value,
    )
    return {"ok": True}


@router.delete("/api/auth/trading/{exchange}")


async def delete_trading_credential(exchange: str, user: User = Depends(auth.get_current_user)):
    auth.delete_trading_credential(user.id, exchange)
    return {"ok": True}


@router.get("/api/auth/futures", response_model=list[FuturesCredentialOut])


async def get_futures_credentials(user: User = Depends(auth.get_current_user)) -> list[FuturesCredentialOut]:
    out = []
    for exchange in SUPPORTED_FUTURES_EXCHANGES_ORDERED:
        adapter = futures_registry.get(exchange)
        cred = user.futures_credentials.get(exchange)
        common = dict(
            exchange=exchange, name=adapter.name, needs_passphrase=adapter.needs_passphrase,
            # Il tetto vero e' il piu' basso fra i due: il frontend non deve
            # mai mostrare uno slider che arriva piu' in alto di quanto il
            # server accetterebbe davvero (vedi i controlli su MAX_FUTURES_LEVERAGE
            # in questo stesso file e in auth.set_futures_credential).
            max_leverage=min(MAX_FUTURES_LEVERAGE, adapter.max_leverage),
            isolated_margin=adapter.isolated_margin,
            key_hint=adapter.key_hint,
        )
        if cred is None:
            out.append(FuturesCredentialOut(
                **common, api_key_set=False, api_secret_set=False, passphrase_set=False,
                enabled=False, size_mode="fixed_usdt", size_value=50.0, leverage=1,
            ))
        else:
            out.append(FuturesCredentialOut(
                **common, api_key_set=bool(cred.api_key_enc), api_secret_set=bool(cred.api_secret_enc),
                passphrase_set=bool(cred.passphrase_enc),
                enabled=cred.enabled, size_mode=cred.size_mode, size_value=cred.size_value, leverage=cred.leverage,
                created_at=cred.created_at,
            ))
    return out


@router.patch("/api/auth/futures")


async def set_futures_credential_endpoint(body: FuturesCredentialRequest, user: User = Depends(auth.get_current_user)):
    if body.exchange not in SUPPORTED_FUTURES_EXCHANGES_ORDERED:
        raise HTTPException(status_code=400, detail=f"Futures not supported yet for this exchange: {body.exchange}")
    adapter = futures_registry.get(body.exchange)
    existing_cred = user.futures_credentials.get(body.exchange)
    has_passphrase = bool(body.passphrase) or bool(existing_cred and existing_cred.passphrase_enc)
    if adapter.needs_passphrase and not has_passphrase:
        raise HTTPException(status_code=400, detail=f"{adapter.name} requires a passphrase in addition to key and secret")
    # Il tetto e' quello di questo exchange, non il massimo globale: un
    # cursore che arriva a 10x su un adattatore che ne sopporta solo 5 deve
    # fermarsi prima di raggiungere il server, ma il server e' l'ultima
    # parola in ogni caso.
    max_lev = min(MAX_FUTURES_LEVERAGE, adapter.max_leverage)
    if not (1 <= body.leverage <= max_lev):
        raise HTTPException(status_code=400, detail=f"Leverage {body.leverage}x outside {adapter.name}'s limit (1-{max_lev}x)")
    try:
        auth.set_futures_credential(
            user.id, body.exchange, body.api_key, body.api_secret,
            body.enabled, body.size_mode, body.size_value, body.leverage,
            passphrase=body.passphrase,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@router.delete("/api/auth/futures/{exchange}")


async def delete_futures_credential(exchange: str, user: User = Depends(auth.get_current_user)):
    auth.delete_futures_credential(user.id, exchange)
    return {"ok": True}


class ReauthRequest(BaseModel):
    password: str = ""


def _require_password(user: User, password: str, request: Request) -> None:
    """A session alone must not enrol the factor that later approves a key
    export (audit 29/09, S05): adding or removing a passkey asks for the
    account password again, under the same lockout as the login."""
    ip_key, user_key = f"ip:{_client_ip(request)}", f"reauth:{user.id}"
    if rate_limit.is_locked(ip_key) or rate_limit.is_locked(user_key):
        raise HTTPException(status_code=429, detail="Too many failed attempts. Try again in a few minutes.")
    if not password or auth.verify_login(user.email, password) is None:
        rate_limit.register_failure(ip_key)
        rate_limit.register_failure(user_key)
        raise HTTPException(status_code=401, detail="Wrong password")
    rate_limit.register_success(user_key)


def _notify_passkey_change(user: User, what: str) -> None:
    text = f"Passkey {what} sull'account {user.email}. Se non sei stato tu, cambia password e sposta i fondi."
    _fire(telegram.send_infra_alert(text))
    if user.telegram_chat_id and _telegram_allowed(user):
        _fire(telegram.send_to(user.telegram_chat_id, text))


@router.post("/api/auth/passkey/register/options")


async def passkey_register_options(
    request: Request, body: ReauthRequest | None = None, user: User = Depends(auth.get_current_user),
) -> dict:
    """Chiamata da un browser gia' loggato — e' l'iscrizione, non il
    flusso di rivelazione: Face ID/Touch ID qui autorizza solo se stesso
    a diventare un modo per approvare in futuro, non rivela niente.
    The password is asked again first: the state token is only handed out
    after it, so /verify is bound to it."""
    _require_password(user, body.password if body else "", request)
    options_json, state_token = onchain_passkey.start_registration(user)
    return {"options": options_json, "state_token": state_token}


class PasskeyRegisterVerifyRequest(BaseModel):
    state_token: str
    credential: dict
    label: str = ""


@router.post("/api/auth/passkey/register/verify")


async def passkey_register_verify(
    body: PasskeyRegisterVerifyRequest, user: User = Depends(auth.get_current_user)
) -> dict:
    try:
        passkey = onchain_passkey.finish_registration(user, body.state_token, body.credential, body.label)
    except Exception as exc:  # noqa: BLE001 — errore di verifica WebAuthn, mai un 500 muto
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    auth.add_passkey(user.id, passkey)
    _notify_passkey_change(user, f"aggiunta ({passkey.label or 'senza nome'})")
    return {"ok": True, "label": passkey.label or "Passkey", "created_at": passkey.created_at}


@router.get("/api/auth/passkey")


async def list_passkeys(user: User = Depends(auth.get_current_user)) -> list[dict]:
    return [{"credential_id": pk.credential_id, "label": pk.label, "created_at": pk.created_at} for pk in user.passkeys]


@router.delete("/api/auth/passkey/{credential_id}")


async def remove_passkey(
    credential_id: str, request: Request, body: ReauthRequest | None = None,
    user: User = Depends(auth.get_current_user),
) -> dict:
    _require_password(user, body.password if body else "", request)
    auth.delete_passkey(user.id, credential_id)
    _notify_passkey_change(user, "rimossa")
    return {"ok": True}
