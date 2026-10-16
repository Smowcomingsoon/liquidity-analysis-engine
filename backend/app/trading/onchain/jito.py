"""Invio via Jito bundle (Solana) — dal piano MEV: "instradare via Jito
bundle con tip dinamico e slippage stretto [...] bundle atomici riducono
drasticamente la finestra di front-run".

Verificato dal vivo (WebFetch su docs.jito.wtf/lowlatencytxnsend/, 23/09/2026):
endpoint reale `https://mainnet.block-engine.jito.wtf:443/api/v1/bundles`,
metodo JSON-RPC `sendBundle`, params = [lista di transazioni firmate
(stringhe), {"encoding": "base64"}] — encoding base64 e' quello
raccomandato dalla doc, base58 e' deprecato/legacy. Risposta: bundle_id
(SHA-256 delle signature) in result. I 8 tip account ufficiali sotto sono
presi dalla stessa pagina.

NON verificato dal vivo: nessun invio reale di un bundle con fondi veri in
questa sessione (servirebbe una tip transaction reale, quindi lamports
veri sul wallet) — verificata solo la FORMA della richiesta (URL, metodo,
struttura dei params, encoding) contro la documentazione reale, con un
test che mocka la risposta HTTP. Il codice che segue non ha mai parlato
con endpoint diversi da quello vero: se send_bundle_via_jito viene
chiamata con fondi veri e non risponde in modo sensato, il chiamante
(execute.py) ricade sull'invio diretto via send_signed_solana_tx.
"""
import base64


from app import http_client

JITO_BLOCK_ENGINE_URL = "https://mainnet.block-engine.jito.wtf:443/api/v1/bundles"

# 8 tip account ufficiali (docs.jito.wtf/lowlatencytxnsend/, verificato
# dal vivo 23/09/2026) — un bundle deve includere una tip transaction verso
# UNO di questi, scelto qui in modo pseudo-casuale per non concentrare il
# traffico su un solo account (nessuna logica di "quale sia migliore":
# sono equivalenti per costruzione, servono solo a parallelizzare i bundle
# fra le pipeline di processing di Jito).
JITO_TIP_ACCOUNTS = [
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
]

# Dalla doc: 1000 lamports e' il minimo tecnico, ma "durante periodi di alta
# domanda potrebbe non bastare" — da qui il tip dinamico invece di un fisso.
JITO_MIN_TIP_LAMPORTS = 1_000
JITO_MAX_TIP_LAMPORTS = 200_000
# Proporzione del notional usata come base del tip: una frazione piccola
# e dichiarata (0.05%), non una cifra a caso — scala con la size dell'ordine
# invece di essere identica per un trade da 5$ e uno da 5000$.
JITO_TIP_NOTIONAL_BPS = 5  # 0.05%
LAMPORTS_PER_SOL = 1_000_000_000

# Slippage stretto per gli ordini instradati via Jito, dal piano: "slippage
# stretto (non l'ampia tolleranza da retail)". Il default di jupiter_quote
# altrove e' 50 bps (0.5%, la tolleranza tipica di un utente retail su
# un'app wallet). Un bundle atomico Jito riduce la finestra di front-run
# fra quote e firma, quindi non serve compensare con un margine di
# slippage largo: 20 bps (0.2%) — abbastanza per il rumore normale di
# prezzo nel tempo di un blocco, non cosi' largo da lasciare margine a un
# sandwich che riuscisse comunque a inserirsi nel bundle.
JITO_SLIPPAGE_BPS = 20


def compute_dynamic_tip_lamports(
    notional_usd: float, sol_price_usd: float,
    min_lamports: int = JITO_MIN_TIP_LAMPORTS,
    max_lamports: int = JITO_MAX_TIP_LAMPORTS,
    bps: int = JITO_TIP_NOTIONAL_BPS,
) -> int:
    """Tip dinamico invece di un valore fisso: proporzionale al notional
    dell'ordine (in SOL, cosi' il tip e' nella stessa unita' della
    transazione che paga), con un minimo (il piede della doc, 1000
    lamports) e un massimo (per non regalare piu' del ragionevole su un
    ordine enorme) entrambi configurabili da chi chiama."""
    if notional_usd <= 0 or sol_price_usd <= 0:
        return min_lamports
    notional_sol = notional_usd / sol_price_usd
    tip = int(notional_sol * LAMPORTS_PER_SOL * bps / 10_000)
    return max(min_lamports, min(tip, max_lamports))


def pick_tip_account(rng=None) -> str:
    """Un tip account a caso fra gli 8 ufficiali — non una preferenza fissa,
    per non concentrare il traffico di questo wallet sempre sullo stesso
    account (dal piano: bundle atomici, non un pattern riconoscibile)."""
    import random
    rng = rng or random.Random()
    return rng.choice(JITO_TIP_ACCOUNTS)


def build_tip_transfer_instruction_data(tip_lamports: int) -> bytes:
    """Dati dell'istruzione System Program Transfer (index 2, u32 LE) +
    lamports (u64 LE) — la stessa codifica usata per QUALSIASI transfer
    SOL nativo, non specifica di Jito: il bundle Jito non richiede un
    formato di istruzione speciale, solo che una delle transazioni del
    bundle trasferisca lamports a uno degli 8 tip account sopra."""
    return (2).to_bytes(4, "little") + tip_lamports.to_bytes(8, "little")


async def send_bundle_via_jito(signed_tx_bytes: bytes, tip_tx_bytes: bytes) -> str:
    """Manda un bundle reale (2 transazioni: lo swap firmato + la tip
    transaction) a mainnet.block-engine.jito.wtf via sendBundle.

    tip_tx_bytes va costruita e firmata da chi chiama (serve la stessa
    chiave del wallet che paga il tip, e questo modulo non tocca chiavi
    private di proposito, come execution.py) — questa funzione si limita
    a incapsulare correttamente la richiesta HTTP secondo la doc reale.

    Ritorna il bundle_id. Non garantisce che il bundle sia stato incluso in
    un blocco (Jito docs: usare getBundleStatuses per quello, non ancora
    implementato qui — fuori dallo scopo di "instradare via bundle")."""
    encoded = [
        base64.b64encode(signed_tx_bytes).decode(),
        base64.b64encode(tip_tx_bytes).decode(),
    ]
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "sendBundle",
        "params": [encoded, {"encoding": "base64"}],
    }
    async with http_client.pooled(timeout=15.0) as client:
        resp = await client.post(JITO_BLOCK_ENGINE_URL, json=payload)
    if resp.status_code != 200:
        raise RuntimeError(f"Jito sendBundle ha risposto {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"Jito sendBundle: {data['error']}")
    return data["result"]  # bundle_id
