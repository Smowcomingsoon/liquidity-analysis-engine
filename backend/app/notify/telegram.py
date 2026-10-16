"""
Notifiche push via Telegram Bot API — https://core.telegram.org/bots/api.
Bot personale creato via @BotFather, nessun servizio terzo, gratuito.

Setup (una tantum, fuori da questo codice):
  1. Su Telegram: cerca @BotFather -> /newbot -> segui i passaggi, copia il token.
  2. Manda un messaggio qualsiasi al bot appena creato (serve per trovare il chat_id).
  3. Metti TELEGRAM_BOT_TOKEN in backend/.env; per il chat_id apri nel browser
     https://api.telegram.org/bot<TOKEN>/getUpdates dopo il passo 2 e leggi "chat":{"id": ...}.
  4. Metti quell'id in TELEGRAM_CHAT_IDS in backend/.env (più id separati da virgola
     se vuoi notificare più persone/chat).

Due canali distinti, non uno
----------------------------
Le notifiche di questo progetto sono di due tipi, e non si leggono allo stesso
modo: un segnale operativo si guarda subito, un "nuovo deploy completato" no.

  * **Alert di trading** (ingressi, uscite, alert di prezzo) -> `send_to()`,
    alla chat PERSONALE di ciascun utente (`User.telegram_chat_id`, che ognuno
    imposta dall'app). Sono per chi opera.
  * **Messaggi di sistema** (deploy, errori 500) -> `send_message()`, alle chat
    in `TELEGRAM_DEV_CHAT_IDS` — o, se non impostata, a `TELEGRAM_CHAT_IDS`
    come è sempre stato. Sono per chi sviluppa.

Se le due finiscono nella stessa conversazione è solo perché puntano allo
stesso chat_id: basta un gruppo Telegram nuovo (stesso bot, nessun bot da
ricreare) il cui id va in `TELEGRAM_DEV_CHAT_IDS`. Per trovarlo:
`.venv/bin/python -m tools.telegram_chat_ids`.
"""

import html
import logging
import re

import httpx

from app import http_client

from app.config import settings

logger = logging.getLogger("telegram_notify")

API_BASE = "https://api.telegram.org"


def _split(raw: str) -> list[str]:
    return [c.strip() for c in raw.split(",") if c.strip()]


def _chat_ids() -> list[str]:
    """Le chat dei messaggi di sistema (deploy, errori 500).

    `TELEGRAM_DEV_CHAT_IDS` vince quando c'e'; altrimenti si ricade su
    `TELEGRAM_CHAT_IDS`, che e' dove finivano prima — cosi' separare i due
    canali e' una riga in `.env`, non una migrazione.
    """
    return _split(settings.telegram_dev_chat_ids) or _split(settings.telegram_chat_ids)


def signal_only_chats() -> set[str]:
    """Chats reserved to strategy signals: `TELEGRAM_SIGNAL_ONLY_CHATS` plus
    every account's alerts chat. Nothing else is ever sent there — not the
    system messages below, not optimizations, gems or listings (main.py
    `_info_chat`)."""
    out = set(_split(settings.telegram_signal_only_chats))
    try:
        from app import auth
        out |= {u.telegram_signal_chat_id for u in auth.list_users() if u.telegram_signal_chat_id}
    except Exception:  # noqa: BLE001 — never block a message on this lookup
        logger.exception("telegram: accounts not readable for the signal-only chats")
    return out


def is_configured() -> bool:
    return bool(settings.telegram_bot_token) and bool(_chat_ids())


async def send_to(chat_id: str, text: str) -> bool:
    """Send to one specific chat — used for per-user watchlist alerts now
    that each user has their own chat_id, instead of broadcasting to every
    configured recipient."""
    if not settings.telegram_bot_token or not chat_id:
        return False
    async with http_client.pooled(timeout=15.0) as client:
        return await _deliver(client, chat_id, text)


# Telegram rifiuta i messaggi oltre 4096 caratteri: si spezza prima, sulle
# righe, cosi' un tag HTML non resta mai a meta' fra due pezzi.
MAX_LEN = 4096


def chunks(text: str, limit: int = MAX_LEN) -> list[str]:
    """Il testo in pezzi da al massimo `limit` caratteri, tagliati a fine riga.

    Lorenzo: "i messaggi su Telegram arrivano tagliati, non sono mai
    completi". Un messaggio lungo va consegnato in piu' pezzi, non accorciato.
    """
    out: list[str] = []
    cur = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                out.append(cur)
                cur = ""
            cut = line.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            out.append(line[:cut])
            line = line[cut:].lstrip(" ")
        candidate = f"{cur}\n{line}" if cur else line
        if len(candidate) > limit:
            out.append(cur)
            cur = line
        else:
            cur = candidate
    if cur.strip():
        out.append(cur)
    return out or [text]


def plain(text: str) -> str:
    """Il testo senza tag HTML, per rimandare un messaggio che Telegram non
    ha saputo leggere (un "<" dentro un errore o un nome di token)."""
    return html.unescape(re.sub(r"</?(?:b|i|u|s|code|pre|a)(?:\s[^>]*)?>", "", text))


async def _post(client: httpx.AsyncClient, chat_id: str, text: str, parse_html: bool) -> httpx.Response:
    body = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if parse_html:
        body["parse_mode"] = "HTML"
    return await client.post(f"{API_BASE}/bot{settings.telegram_bot_token}/sendMessage", json=body)


async def _deliver(client: httpx.AsyncClient, chat_id: str, text: str) -> bool:
    """Manda tutto il testo, in piu' messaggi se serve. Un pezzo con HTML non
    valido viene rimandato come testo semplice invece di andare perso."""
    try:
        for part in chunks(text):
            resp = await _post(client, chat_id, part, True)
            if resp.status_code == 400:
                resp = await _post(client, chat_id, plain(part), False)
            resp.raise_for_status()
        return True
    except httpx.HTTPError:
        logger.warning("Invio Telegram fallito per chat_id=%s", chat_id, exc_info=True)
        return False


async def send_infra_alert(message: str) -> bool:
    """Alert di infrastruttura on-chain (kill switch, circuit breaker,
    RPC degradato, riconciliazione disallineata) — piano SRE punto 4.

    Un canale Telegram FISICAMENTE separato dagli alert di trading
    richiederebbe una seconda configurazione (un secondo bot o un secondo
    chat_id dedicato) che oggi non esiste in `settings` e che questa
    sessione non ha modo di testare dal vivo (nessuna chat di test in
    piu' da verificare) — non va fabbricata una variabile a caso. La
    soluzione minima e onesta: manda con `send_message()` (le stesse chat
    di sistema di sempre, `TELEGRAM_DEV_CHAT_IDS`/`TELEGRAM_CHAT_IDS`) ma
    con un prefisso fisso "⚙️ INFRA:" cosi' e' visivamente distinguibile
    dagli altri messaggi di sistema nella stessa conversazione. Se in
    futuro si aggiunge un secondo bot/chat_id dedicato, questa funzione e'
    l'unico punto da cambiare per farlo diventare un canale vero."""
    return await send_message(f"⚙️ INFRA: {message}")


async def send_message(text: str) -> bool:
    """Manda a tutti i destinatari configurati. Non solleva mai — un errore qui non
    deve mai far fallire l'analisi in corso. Un destinatario che fallisce (es. ha
    bloccato il bot) non impedisce l'invio agli altri."""
    if not is_configured():
        return False

    sent_any = False
    async with http_client.pooled(timeout=15.0) as client:
        reserved = signal_only_chats()
        for chat_id in _chat_ids():
            if chat_id in reserved:
                continue    # a signals-only chat never gets system messages
            # Un destinatario che fallisce non ferma gli altri.
            sent_any = await _deliver(client, chat_id, text) or sent_any
    return sent_any
