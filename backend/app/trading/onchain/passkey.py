"""WebAuthn (Face ID/Touch ID su iPhone, biometria/PIN su Android) come
alternativa alla password per approvare da un secondo dispositivo la
rivelazione di una chiave on-chain. La password resta come rete di
sicurezza: un passkey che non c'e' o non funziona non deve bloccare
l'accesso al proprio stesso wallet.

Isolato apposta (stesso principio degli altri moduli sotto trading/onchain):
non tocca mai la password, non tocca mai una chiave privata — solo
credenziali pubbliche WebAuthn, che da sole non autenticano nessuno.
"""
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import webauthn
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from app.config import settings
from app.models import Passkey, User

_CHALLENGE_TTL_S = 300


def rp_id() -> str:
    return urlparse(settings.public_base_url).hostname or "localhost"


def _origin() -> str:
    return settings.public_base_url.rstrip("/")


RP_NAME = "Liquidity Engine"


@dataclass
class _Ceremony:
    challenge: bytes
    user_id: str
    created_at: float = field(default_factory=time.time)


_ceremonies: dict[str, _Ceremony] = {}


def _purge_expired() -> None:
    now = time.time()
    expired = [t for t, c in _ceremonies.items() if now - c.created_at > _CHALLENGE_TTL_S]
    for t in expired:
        del _ceremonies[t]


def _store_ceremony(challenge: bytes, user_id: str) -> str:
    _purge_expired()
    state_token = secrets.token_urlsafe(24)
    _ceremonies[state_token] = _Ceremony(challenge=challenge, user_id=user_id)
    return state_token


def _pop_ceremony(state_token: str, user_id: str) -> bytes | None:
    _purge_expired()
    c = _ceremonies.pop(state_token, None)
    if c is None or c.user_id != user_id:
        return None
    return c.challenge


def start_registration(user: User) -> tuple[str, str]:
    """Chiamata da un browser gia' loggato (stesso account, stessa
    sessione) — non e' il flusso di rivelazione, e' l'iscrizione del
    passkey che quel flusso user' poi. Ritorna (opzioni JSON per
    navigator.credentials.create(), state_token da rimandare a finish)."""
    options = webauthn.generate_registration_options(
        rp_id=rp_id(),
        rp_name=RP_NAME,
        user_name=user.email,
        user_id=user.id.encode(),
        user_display_name=user.email,
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=webauthn.base64url_to_bytes(pk.credential_id))
            for pk in user.passkeys
        ],
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,  # niente Face ID/Touch ID, niente registrazione
        ),
    )
    state_token = _store_ceremony(options.challenge, user.id)
    return webauthn.options_to_json(options), state_token


def finish_registration(user: User, state_token: str, credential: dict, label: str = "") -> Passkey:
    challenge = _pop_ceremony(state_token, user.id)
    if challenge is None:
        raise ValueError("Registration expired or invalid — try again.")
    verified = webauthn.verify_registration_response(
        credential=credential,
        expected_challenge=challenge,
        expected_rp_id=rp_id(),
        expected_origin=_origin(),
    )
    return Passkey(
        credential_id=webauthn.helpers.bytes_to_base64url(verified.credential_id),
        public_key=webauthn.helpers.bytes_to_base64url(verified.credential_public_key),
        sign_count=verified.sign_count,
        label=label[:60],
        created_at=time.time(),
    )


def start_authentication(user: User) -> tuple[str, str]:
    """Chiamata dalla pagina sul TELEFONO, senza sessione — l'identita' a
    quel punto e' solo 'l'email che ha scritto', verificata dopo dal fatto
    che la firma torna valida per UNO dei passkey di QUELL'utente."""
    options = webauthn.generate_authentication_options(
        rp_id=rp_id(),
        allow_credentials=[
            PublicKeyCredentialDescriptor(id=webauthn.base64url_to_bytes(pk.credential_id))
            for pk in user.passkeys
        ],
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    state_token = _store_ceremony(options.challenge, user.id)
    return webauthn.options_to_json(options), state_token


def finish_authentication(user: User, state_token: str, credential: dict) -> int:
    """Ritorna il nuovo sign_count da salvare (auth.py se ne occupa) o
    lancia ValueError. Un sign_count che non cresce e' come lo intende lo
    standard: un segnale di una chiave clonata, non un dettaglio da
    ignorare — verify_authentication_response lo controlla da solo."""
    challenge = _pop_ceremony(state_token, user.id)
    if challenge is None:
        raise ValueError("Verification expired or invalid — try again.")
    passkey = next((pk for pk in user.passkeys if pk.credential_id == credential.get("id", "")), None)
    if passkey is None:
        raise ValueError("This passkey is not registered on this account.")
    verified = webauthn.verify_authentication_response(
        credential=credential,
        expected_challenge=challenge,
        expected_rp_id=rp_id(),
        expected_origin=_origin(),
        credential_public_key=webauthn.base64url_to_bytes(passkey.public_key),
        credential_current_sign_count=passkey.sign_count,
    )
    return verified.new_sign_count
