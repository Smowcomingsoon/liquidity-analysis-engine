"""Passkey (Face ID/Touch ID) come alternativa alla password per
approvare la rivelazione di una chiave on-chain da un secondo dispositivo.

Quello che NON si puo' testare qui senza un autenticatore vero: la firma
biometrica stessa — verify_registration_response/verify_authentication_response
si aspettano un attestation/assertion object CBOR firmato da una chiave
privata che vive solo nell'elemento sicuro di un telefono reale.
Quello che questi test verificano davvero: le opzioni generate sono valide
e coerenti con la configurazione (rp id, challenge), e il negozio delle
challenge effimere (unico pezzo scritto da zero qui) si comporta come
reveal_requests — uso singolo, scoped all'utente, scadenza reale."""

from __future__ import annotations

import time

from app.config import settings
from app.models import User
from app.trading.onchain import passkey as pk


def _user(passkeys=None) -> User:
    return User(id="u1", email="a@b.com", password_hash="x", passkeys=passkeys or [])


def test_rp_id_matches_configured_public_base_url(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://liquidityengine.app")
    assert pk.rp_id() == "liquidityengine.app"


def test_start_registration_produces_valid_options_and_state():
    user = _user()
    options_json, state_token = pk.start_registration(user)
    assert state_token
    assert '"challenge"' in options_json
    assert pk.rp_id() in options_json


def test_start_authentication_produces_valid_options_even_with_no_passkeys():
    # main.py blocca questo caso a monte (400 prima di chiamare qui) —
    # il modulo stesso deve comunque restare coerente: allow_credentials
    # vuoto e' un'opzione valida dello standard, non un errore da lanciare.
    options_json, state_token = pk.start_authentication(_user())
    assert state_token
    assert '"challenge"' in options_json


def test_finish_registration_rejects_unknown_state_token():
    user = _user()
    try:
        pk.finish_registration(user, "not-a-real-token", {"id": "x"})
        assert False, "doveva lanciare ValueError"
    except ValueError:
        pass


def test_finish_authentication_rejects_credential_not_registered():
    user = _user()
    _, state_token = pk.start_authentication(user)
    try:
        pk.finish_authentication(user, state_token, {"id": "un-passkey-mai-visto"})
        assert False, "doveva lanciare ValueError"
    except ValueError as exc:
        assert "not registered" in str(exc)


def test_ceremony_is_single_use(monkeypatch):
    user = _user()
    _, state_token = pk.start_registration(user)
    # consumato una prima volta (fallisce sulla verifica della firma, ma
    # il pop della ceremony avviene comunque prima di quel controllo)
    try:
        pk.finish_registration(user, state_token, {"id": "x"})
    except Exception:
        pass
    try:
        pk.finish_registration(user, state_token, {"id": "x"})
        assert False, "un secondo uso dello stesso state_token doveva fallire"
    except ValueError as exc:
        assert "expired" in str(exc) or "invalid" in str(exc)


def test_ceremony_expires(monkeypatch):
    user = _user()
    _, state_token = pk.start_registration(user)
    future = time.time() + pk._CHALLENGE_TTL_S + 1
    monkeypatch.setattr(time, "time", lambda: future)
    try:
        pk.finish_registration(user, state_token, {"id": "x"})
        assert False, "doveva essere scaduta"
    except ValueError:
        pass


def test_ceremony_scoped_to_its_own_user():
    owner = _user()
    owner = owner.model_copy(update={"id": "owner"})
    _, state_token = pk.start_registration(owner)
    intruder = _user().model_copy(update={"id": "intruder"})
    try:
        pk.finish_registration(intruder, state_token, {"id": "x"})
        assert False, "un altro utente non deve poter usare questo state_token"
    except ValueError:
        pass
