"""Fase 1 del piano wallet on-chain: generare la chiave, non firmare niente.

Una chiave per chain, mai importata da fuori — nasce qui, cifrata subito
dall'unico chiamante (app.auth) prima di toccare disco. La firma vera
(swap reali) e' un modulo a parte, ancora da scrivere (Fase 3 del piano):
tenerla fuori da qui e' apposta, cosi' un bug nella sola generazione non
puo' mai spostare un soldo.
"""
from dataclasses import dataclass

import base58
from eth_account import Account
from solders.keypair import Keypair

from app.models import OnchainChain


@dataclass
class NewKey:
    address: str
    private_key_hex: str  # sempre esadecimale, indipendentemente dalla chain — un solo formato da cifrare/decifrare


def generate(chain: OnchainChain) -> NewKey:
    if chain == "solana":
        kp = Keypair()
        # bytes(kp) sono i 64 byte "secret key" nel formato che usa anche
        # il CLI solana-keygen (32 seed + 32 pubkey) — from_bytes() li
        # rilegge esattamente cosi'.
        return NewKey(address=str(kp.pubkey()), private_key_hex=bytes(kp).hex())
    if chain == "robinhood":
        # Chain EVM (Arbitrum Orbit, chain id 4663): stessa curva/formato
        # chiave di qualunque altra chain EVM, nessun adattamento specifico.
        acct = Account.create()
        return NewKey(address=acct.address, private_key_hex=acct.key.hex())
    raise ValueError(f"Unsupported chain: {chain}")


def to_import_format(chain: OnchainChain, private_key_hex: str) -> str:
    """La chiave interna e' sempre esadecimale, ma nessun wallet vero la
    vuole cosi': Phantom (e ogni altro wallet Solana) importa il Base58
    dei 64 byte, MetaMask (e ogni wallet EVM) l'esadecimale con "0x" —
    per questo un wallet generato qui non e' un vicolo cieco, si porta
    fuori dal sito su un'app vera in qualunque momento."""
    if chain == "solana":
        return base58.b58encode(bytes.fromhex(private_key_hex)).decode()
    if chain == "robinhood":
        return private_key_hex if private_key_hex.startswith("0x") else f"0x{private_key_hex}"
    raise ValueError(f"Unsupported chain: {chain}")


def address_from_private_key(chain: OnchainChain, private_key_hex: str) -> str:
    """Non usata per generare (generate() gia' restituisce l'indirizzo) —
    serve solo a verificare che una chiave decifrata corrisponda ancora
    all'indirizzo salvato, prima di firmare qualcosa con lei."""
    if chain == "solana":
        return str(Keypair.from_bytes(bytes.fromhex(private_key_hex)).pubkey())
    if chain == "robinhood":
        return Account.from_key(private_key_hex).address
    raise ValueError(f"Unsupported chain: {chain}")
