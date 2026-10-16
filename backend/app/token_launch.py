"""Launch of the $ENGN token on pump.fun, by hand and only on purpose.

Lorenzo: "prepara tutto ma non lanciamo niente per ora". Nothing here runs
by itself: no route, no scheduler, no import from main.py except the
read-only `launched()`. It is a command run on the server, inside the
container, where the volume keeps the keys:

    docker compose exec app python -m app.token_launch wallet
    docker compose exec app python -m app.token_launch check
    ENGN_LAUNCH_ARMED=1 docker compose exec -e ENGN_LAUNCH_ARMED app \
        python -m app.token_launch launch --confirm ENGN

- `wallet` creates the launch wallet (once) and prints its address and
  balance. It is a new wallet, not a user's: fund it with ~0.05 SOL.
- `check` builds and signs the create transaction and asks the RPC to
  simulate it. It never sends and never uploads anything.
- `launch` needs both the environment switch and the typed ticker. It
  uploads logo and metadata to pump.fun, simulates again, sends, and writes
  the mint address where the /engn page reads it. It refuses to run twice.

No dev buy: the team starts at 0% (Lorenzo: "io non voglio mettere soldi").
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

import base58
import httpx

from app import http_client
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from app.trading.onchain.storage import data_file
from app.atomic_write import write_text_atomic

NAME = "Liquidity Engine"
SYMBOL = "ENGN"
DESCRIPTION = (
    "On-chain wallet that snipes new launches, ranks serious microcaps and trades "
    "them with trend-following. Long spot only. Live before the token: "
    "liquidityengine.app/engn"
)
WEBSITE = "https://liquidityengine.app/engn"
LOGO = Path(__file__).resolve().parents[2] / "frontend" / "static" / "engn" / "logo.png"

PUMP_IPFS_URL = "https://pump.fun/api/ipfs"
PUMPPORTAL_TX_URL = "https://pumpportal.fun/api/trade-local"
RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
PRIORITY_FEE_SOL = 0.0005
MIN_BALANCE_SOL = 0.03

_CREATOR_FILE = "engn_launch_creator.json"
_RESULT_FILE = "engn_launch.json"


def launched() -> dict | None:
    """The mint the page shows, once the launch really happened."""
    try:
        data = json.loads(data_file(_RESULT_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and data.get("mint") else None


def _write_private(path: Path, payload: dict) -> None:
    write_text_atomic(path, json.dumps(payload, indent=2), encoding="utf-8")
    os.chmod(path, 0o600)


def _creator(create: bool = False) -> Keypair | None:
    path = data_file(_CREATOR_FILE)
    if path.exists():
        return Keypair.from_bytes(base58.b58decode(json.loads(path.read_text())["secret"]))
    if not create:
        return None
    kp = Keypair()
    _write_private(path, {"address": str(kp.pubkey()), "secret": base58.b58encode(bytes(kp)).decode(),
                          "created_at": int(time.time())})
    return kp


async def _rpc(client: httpx.AsyncClient, method: str, params: list) -> dict:
    resp = await client.post(RPC_URL, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    body = resp.json()
    if "error" in body:
        raise RuntimeError(f"{method}: {body['error']}")
    return body["result"]


async def _balance_sol(client: httpx.AsyncClient, kp: Keypair) -> float:
    return (await _rpc(client, "getBalance", [str(kp.pubkey())]))["value"] / 1e9


async def _upload_metadata(client: httpx.AsyncClient, twitter: str, telegram: str) -> str:
    form = {"name": NAME, "symbol": SYMBOL, "description": DESCRIPTION, "website": WEBSITE,
            "twitter": twitter, "telegram": telegram, "showName": "true"}
    files = {"file": ("logo.png", LOGO.read_bytes(), "image/png")}
    resp = await client.post(PUMP_IPFS_URL, data=form, files=files)
    resp.raise_for_status()
    uri = resp.json().get("metadataUri")
    if not uri:
        raise RuntimeError(f"pump.fun did not return a metadata URI: {resp.text[:200]}")
    return uri


def _create_request(creator: Keypair, mint: Keypair, metadata_uri: str) -> dict:
    return {
        "publicKey": str(creator.pubkey()),
        "action": "create",
        "tokenMetadata": {"name": NAME, "symbol": SYMBOL, "uri": metadata_uri},
        "mint": str(mint.pubkey()),
        "denominatedInSol": "true",
        "amount": 0,            # no dev buy
        "slippage": 10,
        "priorityFee": PRIORITY_FEE_SOL,
        "pool": "pump",
    }


async def _signed_create_tx(client: httpx.AsyncClient, creator: Keypair, mint: Keypair, uri: str) -> bytes:
    resp = await client.post(PUMPPORTAL_TX_URL, json=_create_request(creator, mint, uri))
    if resp.status_code != 200:
        raise RuntimeError(f"PumpPortal answered {resp.status_code}: {resp.text[:200]}")
    tx = VersionedTransaction.from_bytes(resp.content)
    return bytes(VersionedTransaction(tx.message, [mint, creator]))


async def _simulate(client: httpx.AsyncClient, raw: bytes) -> dict:
    return (await _rpc(client, "simulateTransaction", [
        base64.b64encode(raw).decode(), {"encoding": "base64", "sigVerify": True},
    ]))["value"]


async def cmd_wallet() -> int:
    kp = _creator(create=True)
    async with http_client.pooled(timeout=20) as client:
        bal = await _balance_sol(client, kp)
    print(f"launch wallet: {kp.pubkey()}\nbalance: {bal:.4f} SOL (needs >= {MIN_BALANCE_SOL})")
    return 0


async def cmd_check() -> int:
    if launched():
        print(f"already launched: {launched()['mint']}")
        return 1
    creator = _creator()
    if creator is None:
        print("no launch wallet yet: run `wallet` first")
        return 1
    mint = Keypair()
    async with http_client.pooled(timeout=30) as client:
        bal = await _balance_sol(client, creator)
        raw = await _signed_create_tx(client, creator, mint, "https://example.invalid/engn-dry-run.json")
        sim = await _simulate(client, raw)
    print(f"wallet {creator.pubkey()} · {bal:.4f} SOL")
    print(f"transaction built and signed: {len(raw)} bytes, mint would be {mint.pubkey()} (dry run, discarded)")
    print("simulation:", "OK" if sim.get("err") is None else f"FAILED {sim['err']}")
    for line in (sim.get("logs") or [])[-6:]:
        print("  ", line)
    if bal < MIN_BALANCE_SOL:
        print(f"fund the wallet with at least {MIN_BALANCE_SOL} SOL before launching")
    print("nothing was sent")
    return 0 if sim.get("err") is None and bal >= MIN_BALANCE_SOL else 1


async def cmd_launch(confirm: str, twitter: str, telegram: str) -> int:
    if os.environ.get("ENGN_LAUNCH_ARMED") != "1" or confirm != SYMBOL:
        print("refused: set ENGN_LAUNCH_ARMED=1 and pass --confirm ENGN")
        return 2
    if launched():
        print(f"refused: already launched, mint {launched()['mint']}")
        return 2
    creator = _creator()
    if creator is None:
        print("no launch wallet yet: run `wallet` first")
        return 1
    mint = Keypair()
    # The mint key goes to disk before anything is sent: if the process dies
    # after sending, the address is still known.
    _write_private(data_file("engn_launch_mint.json"),
                   {"mint": str(mint.pubkey()), "secret": base58.b58encode(bytes(mint)).decode()})
    async with http_client.pooled(timeout=60) as client:
        if await _balance_sol(client, creator) < MIN_BALANCE_SOL:
            print(f"refused: launch wallet has less than {MIN_BALANCE_SOL} SOL")
            return 1
        uri = await _upload_metadata(client, twitter, telegram)
        raw = await _signed_create_tx(client, creator, mint, uri)
        sim = await _simulate(client, raw)
        if sim.get("err") is not None:
            print("refused: simulation failed", sim["err"], *(sim.get("logs") or [])[-6:], sep="\n  ")
            return 1
        signature = (await _rpc(client, "sendTransaction", [
            base64.b64encode(raw).decode(), {"encoding": "base64"},
        ]))
    result = {"mint": str(mint.pubkey()), "signature": signature, "creator": str(creator.pubkey()),
              "metadata_uri": uri, "at": int(time.time())}
    write_text_atomic(data_file(_RESULT_FILE), json.dumps(result, indent=2), encoding="utf-8")
    print(f"launched: https://pump.fun/coin/{result['mint']}\ntx: https://solscan.io/tx/{signature}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.token_launch", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("wallet")
    sub.add_parser("check")
    launch = sub.add_parser("launch")
    launch.add_argument("--confirm", default="")
    launch.add_argument("--twitter", default="")
    launch.add_argument("--telegram", default="")
    args = parser.parse_args(argv)
    if args.cmd == "wallet":
        return asyncio.run(cmd_wallet())
    if args.cmd == "check":
        return asyncio.run(cmd_check())
    return asyncio.run(cmd_launch(args.confirm, args.twitter, args.telegram))


if __name__ == "__main__":
    sys.exit(main())
