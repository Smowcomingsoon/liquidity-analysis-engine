"""Costruzione e firma di uno swap vero — MAI l'invio. L'invio (broadcast)
resta dietro settings.onchain_execution_enabled, che nasce spento (Fase 3
del piano, non ancora accesa): tutto qui sotto e' stato verificato dal vivo
fino alla firma compresa (quote reale di Jupiter, transazione vera
deserializzata e firmata con la chiave dell'account giusto), mai oltre.

La sequenza e' sempre: quote -> guardrail (risk.run_all_checks, gia' fatto
da chi chiama) -> reserve() sul ledger -> firma -> (quando abilitato) invio
-> mark_submitted -> conferma -> mark_confirmed.
"""
import base64

import httpx
from eth_abi import encode as _abi_encode
from eth_account import Account
from eth_utils import to_checksum_address
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.token import ID as SPL_TOKEN_PROGRAM_ID
from solders.token.associated import get_associated_token_address
from solders.transaction import Transaction, VersionedTransaction

from app.trading.onchain import rpc_client

JUPITER_SWAP_URL = "https://lite-api.jup.ag/swap/v1/swap"
ROBINHOOD_RPC = "https://rpc.mainnet.chain.robinhood.com"

# Verificato il 19/09/2026 su due fonti indipendenti e coerenti fra loro:
# developers.uniswap.org/docs/protocols/v3/deployments/v3-robinhood-chain-deployments
# (che riporta anche il Permit2 singleton universale 0x000000...78BA3, identico
# su ogni chain — un controllo di autenticita' in piu') e docs.robinhood.com/chain.
# SwapRouter02 (Uniswap v3), non v2: piu' semplice da incapsulare senza una
# libreria ABI completa, un solo metodo (exactInputSingle) da codificare.
ROBINHOOD_SWAP_ROUTER02 = to_checksum_address("0xcaf681a66d020601342297493863e78c959e5cb2")
# keccak256("exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))")[:4]
# — calcolato con eth_utils.keccak in questa sessione, non a memoria.
_SELECTOR_EXACT_INPUT_SINGLE = bytes.fromhex("04e45aaf")


async def build_signed_solana_swap(private_key_hex: str, quote: dict) -> bytes:
    """Chiede a Jupiter la transazione grezza per QUESTO quote e la firma
    con la chiave del wallet — ritorna i byte pronti per sendTransaction,
    che questo modulo non chiama mai da solo."""
    kp = Keypair.from_bytes(bytes.fromhex(private_key_hex))
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(JUPITER_SWAP_URL, json={
            "quoteResponse": quote["raw"], "userPublicKey": str(kp.pubkey()),
        })
    if resp.status_code != 200:
        raise RuntimeError(f"Jupiter /swap ha risposto {resp.status_code}: {resp.text[:200]}")
    body = resp.json()
    if "swapTransaction" not in body:
        raise RuntimeError(f"Jupiter /swap senza swapTransaction: {body}")
    raw = base64.b64decode(body["swapTransaction"])
    tx = VersionedTransaction.from_bytes(raw)
    signed = VersionedTransaction(tx.message, [kp])
    return bytes(signed)


async def build_signed_solana_tip_tx(private_key_hex: str, tip_account: str, tip_lamports: int) -> bytes:
    """Costruisce e firma la SECONDA transazione di un bundle Jito: un
    trasferimento SOL nativo (System Program, nessun programma custom) dal
    wallet a uno degli 8 tip account ufficiali (vedi jito.py). Serve un
    blockhash recente vero, letto dallo stesso RPC pubblico usato da
    send_signed_solana_tx — non uno fittizio, altrimenti il bundle verrebbe
    scartato per blockhash scaduto."""
    kp = Keypair.from_bytes(bytes.fromhex(private_key_hex))
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash", "params": []}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post("https://api.mainnet-beta.solana.com", json=payload)
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"getLatestBlockhash failed: {data['error']}")
    blockhash = Hash.from_string(data["result"]["value"]["blockhash"])
    ix = transfer(TransferParams(
        from_pubkey=kp.pubkey(), to_pubkey=Pubkey.from_string(tip_account), lamports=tip_lamports,
    ))
    msg = Message.new_with_blockhash([ix], kp.pubkey(), blockhash)
    tx = Transaction.new_unsigned(msg)
    tx.sign([kp], blockhash)
    return bytes(tx)


async def _solana_recent_blockhash() -> Hash:
    """Passa da rpc_client (fallback multi-endpoint), non da un URL fisso —
    a differenza di build_signed_solana_tip_tx, scritta prima che
    rpc_client esistesse: qui non c'e' un motivo per ripetere lo stesso
    limite in codice nuovo."""
    result = await rpc_client.call_rpc("solana", "getLatestBlockhash", [])
    return Hash.from_string(result["value"]["blockhash"])


async def build_signed_solana_native_transfer(private_key_hex: str, to_address: str, lamports: int) -> bytes:
    """Prelievo: un trasferimento SOL nativo puro (System Program), stessa
    istruzione gia' verificata dal vivo in build_signed_solana_tip_tx —
    qui la destinazione e' un indirizzo qualsiasi scelto dall'utente, non
    uno degli 8 tip account fissi di Jito."""
    kp = Keypair.from_bytes(bytes.fromhex(private_key_hex))
    blockhash = await _solana_recent_blockhash()
    ix = transfer(TransferParams(
        from_pubkey=kp.pubkey(), to_pubkey=Pubkey.from_string(to_address), lamports=lamports,
    ))
    msg = Message.new_with_blockhash([ix], kp.pubkey(), blockhash)
    tx = Transaction.new_unsigned(msg)
    tx.sign([kp], blockhash)
    return bytes(tx)


# Instruction discriminator del Token Program (non-checked "Transfer",
# indice 3 nell'enum TokenInstruction) e del Associated Token Account
# Program ("Create", nessun dato — l'unica istruzione che il programma
# offriva alla sua prima versione, quella ancora deployata sull'indirizzo
# canonico). Nessuna libreria spl-token in requirements.txt: solders da'
# solo le primitive (Instruction, Pubkey, l'indirizzo derivato), l'encoding
# della singola istruzione e' pochi byte, non vale una dipendenza in piu'.
_SPL_TRANSFER_INSTRUCTION = 3
ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
SYSTEM_PROGRAM_ID = Pubkey.from_string("11111111111111111111111111111111")


async def build_signed_solana_token_transfer(
    private_key_hex: str, mint_address: str, to_address: str, amount_raw: int,
) -> bytes:
    """Prelievo di un token SPL (non il nativo): trasferisce dal token
    account associato del wallet a quello del destinatario, creandolo
    prima se non esiste ancora — un destinatario che non ha mai ricevuto
    quel token non ha un token account li' ad aspettarlo. La creazione
    dell'account e' verificata solo per costruzione (stesso schema
    dell'Associated Token Account Program, documentato pubblicamente),
    non contro un invio reale in questa sessione — a differenza del
    trasferimento nativo sopra, che ripete un'istruzione gia' vista
    davvero in un bundle Jito andato a buon fine."""
    kp = Keypair.from_bytes(bytes.fromhex(private_key_hex))
    mint = Pubkey.from_string(mint_address)
    to_owner = Pubkey.from_string(to_address)
    source_ata = get_associated_token_address(kp.pubkey(), mint)
    dest_ata = get_associated_token_address(to_owner, mint)

    dest_info = await rpc_client.call_rpc("solana", "getAccountInfo", [str(dest_ata), {"encoding": "base64"}])
    dest_exists = dest_info.get("value") is not None

    instructions = []
    if not dest_exists:
        instructions.append(Instruction(
            program_id=ASSOCIATED_TOKEN_PROGRAM_ID,
            data=bytes(),
            accounts=[
                AccountMeta(kp.pubkey(), is_signer=True, is_writable=True),   # payer
                AccountMeta(dest_ata, is_signer=False, is_writable=True),     # nuovo account associato
                AccountMeta(to_owner, is_signer=False, is_writable=False),    # proprietario del nuovo account
                AccountMeta(mint, is_signer=False, is_writable=False),
                AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
                AccountMeta(SPL_TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
            ],
        ))
    instructions.append(Instruction(
        program_id=SPL_TOKEN_PROGRAM_ID,
        data=bytes([_SPL_TRANSFER_INSTRUCTION]) + amount_raw.to_bytes(8, "little"),
        accounts=[
            AccountMeta(source_ata, is_signer=False, is_writable=True),
            AccountMeta(dest_ata, is_signer=False, is_writable=True),
            AccountMeta(kp.pubkey(), is_signer=True, is_writable=False),
        ],
    ))

    blockhash = await _solana_recent_blockhash()
    msg = Message.new_with_blockhash(instructions, kp.pubkey(), blockhash)
    tx = Transaction.new_unsigned(msg)
    tx.sign([kp], blockhash)
    return bytes(tx)


async def build_signed_evm_native_transfer(private_key_hex: str, to_address: str, amount_wei: int) -> bytes:
    """Prelievo del nativo (ETH) su Robinhood Chain: una transazione EVM
    semplice, nessuna calldata — stesso nonce/gas-price dal vivo di
    build_signed_robinhood_swap, gas_limit 21000 (lo standard di un
    trasferimento nativo, mai piu' di quello per definizione)."""
    account = Account.from_key(private_key_hex)
    nonce_hex = await _robinhood_rpc("eth_getTransactionCount", [account.address, "pending"])
    gas_price_hex = await _robinhood_rpc("eth_gasPrice", [])
    gas_price = (int(gas_price_hex, 16) * 12) // 10  # stesso margine del 20% di build_signed_robinhood_swap
    tx = {
        "nonce": int(nonce_hex, 16),
        "to": to_checksum_address(to_address),
        "value": amount_wei,
        "gas": 21000,
        "gasPrice": gas_price,
        "chainId": 4663,
        "data": "0x",
    }
    return sign_evm_transaction(private_key_hex, tx)


_SELECTOR_ERC20_TRANSFER = bytes.fromhex("a9059cbb")  # keccak256("transfer(address,uint256)")[:4]


async def build_signed_evm_token_transfer(
    private_key_hex: str, token_address: str, to_address: str, amount_raw: int, gas_limit: int = 80_000,
) -> bytes:
    """Prelievo di un token ERC20 su Robinhood Chain: transfer(address,
    uint256) sul contratto del token, value=0 — stesso schema di encoding
    ABI di build_swap_router02_calldata, un solo metodo standard."""
    account = Account.from_key(private_key_hex)
    nonce_hex = await _robinhood_rpc("eth_getTransactionCount", [account.address, "pending"])
    gas_price_hex = await _robinhood_rpc("eth_gasPrice", [])
    gas_price = (int(gas_price_hex, 16) * 12) // 10
    calldata = _SELECTOR_ERC20_TRANSFER + _abi_encode(
        ["address", "uint256"], [to_checksum_address(to_address), amount_raw],
    )
    tx = {
        "nonce": int(nonce_hex, 16),
        "to": to_checksum_address(token_address),
        "value": 0,
        "gas": gas_limit,
        "gasPrice": gas_price,
        "chainId": 4663,
        "data": "0x" + calldata.hex(),
    }
    return sign_evm_transaction(private_key_hex, tx)


async def send_signed_solana_tx(signed_tx_bytes: bytes) -> str:
    """Unico punto che manda davvero una transazione Solana in chain —
    chiamato solo se settings.onchain_execution_enabled e' True, controllo
    che sta a chi chiama, non qui (questa funzione non legge settings di
    proposito: un modulo che decide da solo se sia acceso e' un modulo che
    un domani si dimentica di controllare)."""
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
        "params": [base64.b64encode(signed_tx_bytes).decode(), {"encoding": "base64"}],
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post("https://api.mainnet-beta.solana.com", json=payload)
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"Solana send failed: {data['error']}")
    return data["result"]  # signature


def sign_evm_transaction(private_key_hex: str, tx: dict) -> bytes:
    """Firma una transazione EVM gia' costruita (nonce, gas, to, data, value,
    chainId — 4663 per Robinhood Chain) — non costruisce la chiamata allo
    swap del router Uniswap: quell'encoding ABI specifico richiede
    l'indirizzo vero del router/pool su Robinhood Chain, che questa sessione
    non ha ancora verificato dal vivo. Il pezzo generico (firma di una
    transazione EVM qualsiasi con la chiave del wallet) e' invece reale e
    testato: e' la parte che NON dipende da quale contratto si chiami."""
    signed = Account.sign_transaction(tx, private_key_hex)
    return bytes(signed.raw_transaction)


def build_swap_router02_calldata(
    token_in: str, token_out: str, fee: int, recipient: str,
    amount_in: int, amount_out_minimum: int, sqrt_price_limit_x96: int = 0,
) -> bytes:
    """Codifica ABI di SwapRouter02.exactInputSingle — selettore verificato
    sopra + eth_abi (la libreria standard dell'ecosistema, non una
    reimplementazione fatta a mano dello spec ABI). NON ancora verificata
    contro una chiamata reale a un pool vero su Robinhood Chain: nessun
    indirizzo di pool/fee tier noto in questa sessione per farlo. Chi
    chiama non deve firmare una transazione costruita qui senza aver prima
    controllato fee/amount_out_minimum contro un quote fresco."""
    encoded = _abi_encode(
        ["(address,address,uint24,address,uint256,uint256,uint160)"],
        [(
            to_checksum_address(token_in), to_checksum_address(token_out), fee,
            to_checksum_address(recipient), amount_in, amount_out_minimum, sqrt_price_limit_x96,
        )],
    )
    return _SELECTOR_EXACT_INPUT_SINGLE + encoded


async def send_signed_evm_tx(raw_tx_bytes: bytes) -> str:
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "eth_sendRawTransaction",
        "params": ["0x" + raw_tx_bytes.hex()],
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(ROBINHOOD_RPC, json=payload)
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"Robinhood Chain send failed: {data['error']}")
    return data["result"]  # tx hash


async def _robinhood_rpc(method: str, params: list) -> str:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(ROBINHOOD_RPC, json=payload)
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"RPC Robinhood Chain ({method}): {data['error']}")
    return data["result"]


async def build_signed_robinhood_swap(
    private_key_hex: str, token_in: str, token_out: str, fee: int,
    amount_in: int, amount_out_minimum: int, gas_limit: int = 300_000,
) -> bytes:
    """Costruisce ed firma una vera exactInputSingle su SwapRouter02 —
    nonce e gas price letti dal vivo via RPC (mai inventati), gas_limit
    fisso e prudente (uno swap Uniswap v3 a un solo hop ne usa tipicamente
    150-220k: 300k lascia margine senza essere assurdo). MAI l'invio, per
    lo stesso motivo di build_signed_solana_swap: quello resta dietro
    settings.onchain_execution_enabled, controllato da chi chiama."""
    account = Account.from_key(private_key_hex)
    nonce_hex = await _robinhood_rpc("eth_getTransactionCount", [account.address, "pending"])
    gas_price_hex = await _robinhood_rpc("eth_gasPrice", [])
    calldata = build_swap_router02_calldata(
        token_in, token_out, fee, account.address, amount_in, amount_out_minimum,
    )
    # La base fee si muove blocco per blocco: un gasPrice preso esattamente
    # a questo istante puo' essere gia' sotto quella del blocco in cui la
    # transazione arrivera' davvero (visto in produzione: "maxFeePerGas
    # 50242000 baseFee 50392000", una differenza di frazioni di secondo).
    # +20% di margine assorbe il movimento normale senza sovrapagare in
    # modo assurdo — non serve eth_feeHistory per una L2 con base fee
    # cosi' stabile.
    gas_price = (int(gas_price_hex, 16) * 12) // 10
    tx = {
        "nonce": int(nonce_hex, 16),
        "to": ROBINHOOD_SWAP_ROUTER02,
        "value": 0,
        "gas": gas_limit,
        "gasPrice": gas_price,
        "chainId": 4663,
        "data": "0x" + calldata.hex(),
    }
    return sign_evm_transaction(private_key_hex, tx)
