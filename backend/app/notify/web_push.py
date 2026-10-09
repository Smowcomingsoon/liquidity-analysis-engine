"""
Web Push: a notification on the phone or the desktop when a long task launched
from the app is done, even with the app closed (Lorenzo, 01/10: "qualsiasi
task [...] deve continuare in background [...] e arrivarmi una notifica al
completamento [...] che mi riporta esattamente al punto").

Each browser that allowed notifications is a "device": a random id the app
keeps in localStorage, with the push subscription its service worker got
(frontend/static/sw.js). A job remembers the device that launched it and
notifies that one only: one notification per finished job, never a broadcast.

The VAPID keys sign every push. They come from the environment
(VAPID_PRIVATE_KEY / VAPID_PUBLIC_KEY) when set, otherwise they are made once
and kept on the data volume next to session_secret.txt: changing them
invalidates every subscription, so they must outlive deploys, and they are
never committed.

iPhone: Safari delivers web push only to the app added to the home screen
(iOS 16.4+); in a Safari tab there is no PushManager at all.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from pathlib import Path

from app.atomic_write import write_text_atomic

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
_KEYS_FILE = _DATA_DIR / "vapid_keys.json"
_SUBS_FILE = _DATA_DIR / "push_subscriptions.json"
_MAX_SUBS = 500

_lock = threading.Lock()
_keys: dict | None = None
_subs: dict[str, dict] | None = None


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _make_keys() -> dict:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_numbers().private_value.to_bytes(32, "big")
    public = key.public_key().public_bytes(serialization.Encoding.X962,
                                           serialization.PublicFormat.UncompressedPoint)
    return {"private": _b64(private), "public": _b64(public)}


def keys() -> dict:
    """{"private", "public"}, base64url: the raw 32-byte key and the
    uncompressed point the browser wants as applicationServerKey."""
    global _keys
    with _lock:
        if _keys is None:
            env_priv, env_pub = os.environ.get("VAPID_PRIVATE_KEY", ""), os.environ.get("VAPID_PUBLIC_KEY", "")
            if env_priv and env_pub:
                _keys = {"private": env_priv.strip(), "public": env_pub.strip()}
            else:
                try:
                    _keys = json.loads(_KEYS_FILE.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    _keys = _make_keys()
                    _KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
                    write_text_atomic(_KEYS_FILE, json.dumps(_keys))
                    try:
                        os.chmod(_KEYS_FILE, 0o600)
                    except OSError:
                        pass
        return _keys


def public_key() -> str:
    return keys()["public"]


def _load() -> dict[str, dict]:
    global _subs
    if _subs is None:
        try:
            _subs = json.loads(_SUBS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _subs = {}
    return _subs


def _save() -> None:
    _SUBS_FILE.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(_SUBS_FILE, json.dumps(_subs or {}))


def valid_device(device: str | None) -> bool:
    return bool(device) and 8 <= len(device) <= 64 and all(c.isalnum() or c in "-_" for c in device)


def subscribe(device: str, subscription: dict, user_id: str = "") -> None:
    endpoint = subscription.get("endpoint") if isinstance(subscription, dict) else None
    k = (subscription or {}).get("keys") or {}
    if not valid_device(device) or not isinstance(endpoint, str) or not endpoint.startswith("https://") \
            or not k.get("p256dh") or not k.get("auth"):
        raise ValueError("invalid subscription")
    with _lock:
        subs = _load()
        subs[device] = {"sub": {"endpoint": endpoint, "keys": {"p256dh": k["p256dh"], "auth": k["auth"]}},
                        "user": user_id, "at": time.time()}
        if len(subs) > _MAX_SUBS:
            for old in sorted(subs, key=lambda d: subs[d]["at"])[: len(subs) - _MAX_SUBS]:
                del subs[old]
        _save()


def unsubscribe(device: str) -> None:
    with _lock:
        if _load().pop(device, None) is not None:
            _save()


def subscribed(device: str) -> bool:
    with _lock:
        return device in _load()


def _deliver(sub: dict, payload: dict) -> int:
    """POST one push; the HTTP status (0 = network error)."""
    from pywebpush import WebPushException, webpush

    try:
        r = webpush(subscription_info=sub, data=json.dumps(payload),
                    vapid_private_key=keys()["private"],
                    vapid_claims={"sub": os.environ.get("VAPID_SUBJECT", "mailto:push@liquidityengine.app")},
                    ttl=24 * 3600, timeout=10)
        return getattr(r, "status_code", 201)
    except WebPushException as exc:
        return getattr(exc.response, "status_code", 0) or 0
    except Exception:
        logger.exception("web push failed")
        return 0


def send(device: str, title: str, body: str, url: str, tag: str = "") -> bool:
    """One notification to one device, in a background thread. False when the
    device never subscribed. A subscription the push service says is gone
    (404/410: notifications revoked, app removed) is forgotten."""
    with _lock:
        hit = _load().get(device or "")
    if hit is None:
        return False
    payload = {"title": title[:80], "body": body[:180], "url": url, "tag": tag or url}

    def run() -> None:
        status = _deliver(hit["sub"], payload)
        if status in (404, 410):
            unsubscribe(device)
        elif status and status >= 400:
            logger.warning("web push %s for device %s", status, device[:8])

    threading.Thread(target=run, name="web-push", daemon=True).start()
    return True
