# B00 — Mappa del backend e registro delle evidenze

Piano: issue #357. Audit di partenza: #347 / PR #348 (snapshot `ceef100`).
Base di questo registro: `main` a `1d26b26` (29/09/2026). Aggiornare a ogni PR.

## Mappa (verificata sul codice, non dall'audit)

| area | dove | note |
|---|---|---|
| Endpoint | `app/main.py` (7 897 righe, 186 route, nessun router separato) | 89 in scrittura; 139 con `Depends(auth.get_current_user)` |
| Middleware | `require_authentication` (`main.py:1619`), GZip | porte pubbliche elencate a mano; ospite prima delle porte pubbliche |
| Sessioni | cookie firmato HMAC (`auth.py`), 30 giorni | revoca al logout e logout-all (#359) |
| Utenti, wallet cifrati, chiavi exchange | `data/users.json` (JSON unico) | proprietario: `auth.py`; scritto atomico con `.bak` (#359) |
| Ordini on-chain | SQLite `data/onchain_ledger.db` (WAL) | proprietario: `trading/onchain/ledger.py`; riconciliatore ogni 20 s |
| Progetti / backtest lab | SQLite `onchain_projects.db`, `backtest_lab.sqlite` (WAL) | solo ricerca |
| Altri archivi | ~50 file JSON in `backend/data` | letti in memoria all'avvio, riscritti interi |
| Segreti (env) | `SESSION_SECRET`, `DEEPSEEK_API_KEY`, `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `GITHUB_API_TOKEN`, chiavi MEXC/KuCoin, `X_BEARER_TOKEN` | un solo processo li vede tutti |
| Chiavi wallet | cifrate con `_ONCHAIN_SECRET` (derivato da `SESSION_SECRET`) | decifrate solo in `auth.py:370` |
| Percorsi che firmano | `trading/onchain/{execute,execution,send,twap_jobs,autotrader,keys}.py`, `token_launch.py`, `wallet_link.py` (firma utente) | invarianti comuni in `risk.run_safety_checks` (#350) |
| Task in background | ~25 loop avviati in `lifespan` (`main.py:1514`) | tutti tracciati e cancellati allo spegnimento da questa PR |
| Processi | uno solo (uvicorn, un worker) | con più worker ogni loop partirebbe N volte: nessun lease |

## Registro

Stato: **confermato** (visto nel codice), **risolto** (PR su main con test), **ipotesi** (da verificare).

| ID | problema | gravità | stato | PR | verifica residua |
|---|---|---|---|---|---|
| S01 | wallet spento/breaker non applicati a ogni invio | alta | risolto | #350 | disabilitazione durante un'attesa di rete |
| S02 | importi e prezzi dal client | alta | risolto | #354 | nuovo quote dopo la riduzione (ora rifiuta) |
| S03 | retry che duplica ordini, budget non atomico | alta | risolto | #350, #354 | concorrenza reale con più processi (oggi uno) |
| S04 | ack del nodo preso per conferma | alta | risolto | #354 | fee effettive contabilizzate |
| S05 | una sessione aggiunge la propria passkey e sblocca l'export | alta | risolto | #359 | — |
| S06 | logout non revoca la copia del cookie | media | risolto | #359 | revoca di tutte le sessioni al cambio password |
| S07 | cifratura senza autenticazione, chiave radice condivisa | media | risolto | #359 | `WALLET_ENCRYPTION_KEY` da impostare sul server (Lorenzo) |
| M01/M02 | dato mancante trattato come controllo passato | media | risolto | #367 | — |
| B02-a | 39 scritture di archivi JSON non atomiche (pine esclusa); `users.json` illeggibile = tutti gli utenti persi al salvataggio successivo | alta | risolto | questa | backup/restore non provati |
| B04-a | 7 task di `lifespan` senza riferimento (fra cui il riconciliatore degli ordini) e mai cancellati | media | risolto | questa | ~30 `create_task` "spara e dimentica" per Telegram in `main.py` |
| B04-c | ~30 `create_task` "spara e dimentica" (Telegram) in `main.py`: raccoglibili dal GC, errori mai visti | bassa | risolto | #381 | — |
| A-SSRF | sito del token letto con controllo DNS prima e nuova risoluzione alla connessione (rebinding); Chromium risolveva da solo | media | risolto | #381 | Chromium non e' nell'immagine: oggi il render non gira |
| A-DEPLOY | il deploy faceva `reset --hard origin/main`, non il commit verificato | bassa | risolto | #381 | Actions ferme (B08-a) |
| A-ADMIN | ruolo admin dato dall'email configurata; il tester era admin senza esserlo | media | risolto | questa | registrazione gia' limitata a `registration_allowed_emails`; se il `.env` del server ha `ADMIN_EMAILS` vince lui |
| B04-b | nessun lease sui loop: due processi = scheduler doppi | media | confermato | — | oggi un solo worker; documentato, non cambiare senza lease |
| B08-a | ogni run delle Actions fallisce in ~4 s senza step (CI, deploy, DeepSeek) | alta | confermato | — | causa lato account GitHub (minuti/fatturazione?): la vede solo Lorenzo |
| B06 | `main.py` monolitico, nessun router | bassa | confermato | — | estrarre per flusso, dopo B01-B05 |
| B07 | prestazioni non misurate | — | non verificato | — | — |

## Baseline

Suite: `cd backend && .venv/bin/python -m pytest tests/ -q` (Python 3.12). La CI non gira (B08-a): la baseline e' locale.
