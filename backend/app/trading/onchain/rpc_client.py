"""Fallback multi-RPC con circuit breaker minimo — piano SRE on-chain, punto 1.

Gli endpoint pubblici (Solana, Robinhood Chain) non hanno SLA e sono
rate-limited per definizione: un timeout non e' un'eccezione rara, e' la
normalita' operativa. Questo modulo prova un endpoint primario e, solo se
fallisce per un motivo di TRASPORTO (timeout, connessione rifiutata, DNS...),
prova un secondario — un errore applicativo JSON-RPC (`{"error": ...}` con
una risposta HTTP valida) non e' un problema di endpoint e non fa scattare
il fallback, si propaga subito a chi chiama.

Circuit breaker: dopo `_FAILURE_THRESHOLD` fallimenti di trasporto
CONSECUTIVI su un endpoint, quell'endpoint viene saltato per
`_COOLDOWN_SECONDS` prima di essere riprovato — cosi' un endpoint morto non
raddoppia la latenza di ogni chiamata (primario+secondario entrambi
interrogati) finche' non si riprende da solo.

Secondi endpoint scelti — verificati come reali e pubblici, non inventati:
- **Solana**: `https://rpc.ankr.com/solana` — RPC pubblico gratuito di Ankr,
  nessuna chiave richiesta, ampiamente usato come alternativa al RPC
  ufficiale `api.mainnet-beta.solana.com` proprio perche' quest'ultimo e'
  rate-limited in modo aggressivo.
- **Robinhood Chain**: NESSUN secondo endpoint pubblico trovato. La chain e'
  troppo recente/di nicchia per avere provider terzi con un RPC pubblico
  gratuito equivalente a quelli di Ethereum/Solana (Ankr, publicnode,
  ecc. non la elencano). Piuttosto che inventare un URL plausibile ma
  falso — peggio di nessun fallback, perche' fallirebbe silenziosamente
  sempre — questa chain resta con un solo endpoint: il circuit breaker
  comunque la protegge (salta le chiamate durante il cooldown invece di
  martellare un nodo degradato), ma senza un secondo nodo a cui passare.
  Se un provider pubblico emergesse, va solo aggiunto alla lista sotto.

Migrazione non ancora fatta (onesta, non un errore): questo modulo e' usato
solo per le chiamate di saldo/quote piu' frequenti (`balances.py`,
`quotes.py`). Le chiamate di FIRMA/INVIO in `execution.py` (send_transaction,
eth_sendRawTransaction, eth_getTransactionCount, eth_gasPrice) restano sul
solo endpoint diretto: mandare una transazione a un endpoint diverso da
quello che ha dato il nonce e' un rischio di stato che qui non si vuole
correre senza pensarci con piu' tempo. TODO: valutare se vale la pena anche
per quelle, o se il rischio di doppio invio le rende un caso a parte.
"""
import time

import httpx

_ENDPOINTS: dict[str, list[str]] = {
    "solana": [
        "https://api.mainnet-beta.solana.com",
        "https://rpc.ankr.com/solana",
    ],
    "robinhood": [
        "https://rpc.mainnet.chain.robinhood.com",
        # Nessun secondo endpoint pubblico verificato — vedi il docstring
        # del modulo. Non aggiungere un URL qui senza averlo verificato dal
        # vivo con una vera chiamata RPC.
    ],
}

_FAILURE_THRESHOLD = 3
_COOLDOWN_SECONDS = 60.0

# Stato del circuit breaker per endpoint: {url: {"consecutive_failures": int, "cooldown_until": float}}
_breaker_state: dict[str, dict] = {}


def _state_for(url: str) -> dict:
    return _breaker_state.setdefault(url, {"consecutive_failures": 0, "cooldown_until": 0.0})


def _is_cooling_down(url: str) -> bool:
    return time.time() < _state_for(url)["cooldown_until"]


def _record_success(url: str) -> None:
    state = _state_for(url)
    state["consecutive_failures"] = 0
    state["cooldown_until"] = 0.0


def _record_failure(url: str) -> None:
    state = _state_for(url)
    state["consecutive_failures"] += 1
    if state["consecutive_failures"] >= _FAILURE_THRESHOLD:
        state["cooldown_until"] = time.time() + _COOLDOWN_SECONDS


def circuit_status() -> dict:
    """Stato leggibile per /admin/status: quale endpoint e' attivo (il primo
    non in cooldown) per ciascuna chain, e i fallimenti recenti di ognuno."""
    result = {}
    for chain, urls in _ENDPOINTS.items():
        active = next((u for u in urls if not _is_cooling_down(u)), urls[0])
        result[chain] = {
            "active_endpoint": active,
            "endpoints": [
                {
                    "url": u,
                    "consecutive_failures": _state_for(u)["consecutive_failures"],
                    "cooling_down": _is_cooling_down(u),
                }
                for u in urls
            ],
        }
    return result


async def call_rpc(chain: str, method: str, params: list, timeout: float = 15.0) -> object:
    """Chiama un metodo JSON-RPC su `chain`, con fallback automatico sul
    secondo endpoint (se esiste) quando il primo fallisce per un motivo di
    trasporto. Ritorna il campo "result" della risposta. Solleva RuntimeError
    per un errore applicativo JSON-RPC (nessun fallback: non e' colpa
    dell'endpoint) o se tutti gli endpoint disponibili hanno fallito."""
    urls = _ENDPOINTS.get(chain)
    if not urls:
        raise ValueError(f"Chain not supported by rpc_client: {chain}")

    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    last_transport_error: Exception | None = None

    candidates = [u for u in urls if not _is_cooling_down(u)] or urls[:1]
    for url in candidates:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            # Timeout, connessione rifiutata, DNS, risposta non-JSON, status
            # HTTP di errore: problema di TRASPORTO, il breaker lo conta e
            # si prova il prossimo endpoint.
            _record_failure(url)
            last_transport_error = exc
            continue

        _record_success(url)
        if "error" in data:
            # Errore applicativo JSON-RPC: l'endpoint ha risposto bene, e'
            # la richiesta che e' sbagliata (o il metodo non supportato).
            # Non e' un problema dell'endpoint: nessun fallback.
            raise RuntimeError(f"RPC {chain} ({method}): {data['error']}")
        return data["result"]

    raise RuntimeError(
        f"All RPC endpoints for {chain} failed (last error: {last_transport_error})"
    )
