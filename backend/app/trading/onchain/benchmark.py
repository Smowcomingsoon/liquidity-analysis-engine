"""Il metro di paragone: DREGG.

Lorenzo (24/09): "mi devi mettere drag al primo posto in assoluto con
menzione d'onore per la complessita' tecnica e l'ambizione del progetto
[...] capisci che questo e' il top del top che si puo' trovare a queste
capitalizzazioni, estrai punti chiave da questo progetto e applica come
regola per altri progetti che possono essere simili a questo dal punto di
vista tecnologico".

DREGG (Dragon's Egg, www.dregg.net, github.com/emberian/dregg), letto il
24/09/2026: un sistema operativo distribuito a capability con il kernel
scritto e dimostrato in Lean 4, collegato via FFI a un nodo in Rust; ogni
transizione di stato ha una prova STARK (Plonky3, BabyBear, Poseidon2) con
aggregazione ricorsiva per i light client; consenso proprio (Blocklace DAG,
finalita' BFT); SDK Rust, TypeScript e Python, CLI, playground wasm nel
browser. Oltre 10 000 commit, licenza AGPL, e un sito che dichiara da solo
i propri limiti ("no independent external audit").

Da li' otto tratti, ognuno con il suo peso. Un progetto li guadagna da cio'
che si puo' leggere senza fidarsi della sua parola: linguaggi e cartelle del
repository, argomenti e descrizione su GitHub, numero di commit, testo del
sito. Il risultato (0-100) e' la somiglianza tecnica a DREGG: in classifica
pesa accanto a GitHub, sito e tokenomics, e il radar lo puo' leggere uguale.
"""
from __future__ import annotations

import re

NAME = "DREGG"
CHAIN = "solana"
TOKEN = "XkeTXo1125vz5H9svJpGiw4JvLbN8VmMu9cmMvspump"
WEBSITE = "https://www.dregg.net/"
GITHUB = "https://github.com/emberian/dregg"
HONOR = ("Honorable mention: kernel proven in Lean 4, recursive STARK proofs, its own consensus, "
         "over 10,000 commits. The benchmark for technical complexity and ambition.")

# (chiave, etichetta, peso, cosa cercare). "langs" nei linguaggi GitHub,
# "dirs" fra le cartelle principali, "text" in descrizione, argomenti e sito.
TRAITS = [
    ("formal", "Formal verification", 20,
     {"langs": {"lean", "coq", "isabelle", "agda", "tla", "dafny", "f*", "idris"},
      "text": r"\b(formally verified|formal verification|machine[- ]checked|lean ?4|coq|isabelle|agda|"
              r"tla\+|dafny|proofs? of correctness|verified (kernel|executor|compiler))\b"}),
    ("zk", "Zero-knowledge proofs", 18,
     {"dirs": {"circuit", "circuits", "prover", "zk", "stark", "snark"},
      "text": r"\b(stark|snark|zk-?snark|zk-?stark|zero[- ]knowledge|plonky[23]?|plonk|halo2|groth16|"
              r"risc0|sp1|circom|cairo|noir|recursive proofs?|zkvm|poseidon)\b"}),
    ("systems", "Core in a systems language", 10,
     {"langs": {"rust", "c++", "c", "go", "zig", "ocaml", "haskell", "move", "lean"}}),
    ("protocol", "Own network and consensus", 14,
     {"dirs": {"node", "consensus", "p2p", "network", "blocklace", "gossip", "validator"},
      "text": r"\b(consensus|bft|byzantine|dag|gossip|p2p|peer[- ]to[- ]peer|libp2p|light clients?|"
              r"validators?|finality)\b"}),
    ("architecture", "New architecture, not a fork", 10,
     {"dirs": {"kernel", "vm", "runtime", "compiler", "executor", "metatheory", "spec"},
      "text": r"\b(object[- ]capabilit\w+|capability[- ]based|operating system|kernel|virtual machine|"
              r"compiler|executor|semantic|type system|runtime)\b"}),
    ("tooling", "SDK, CLI and a runnable node", 10,
     {"dirs": {"sdk", "sdks", "cli", "bin", "crates", "packages", "examples"},
      "text": r"\b(sdk|cli|wasm|playground|devnet|testnet|faucet)\b"}),
    ("depth", "Long commit history", 12, {}),
    ("candor", "Openly stated limits", 6,
     {"text": r"(no (independent )?(external )?audit|not (yet )?audited|unaudited|research software|"
              r"active research|known (limits|gaps|issues)|open gaps|named limit|not ready for)"}),
]

DEPTH_FULL = 5_000     # commit: da qui il tratto "storia lunga" e' pieno...
DEPTH_MIN = 300        # ...sotto questi non conta


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def site_terms(text: str) -> dict[str, list[str]]:
    """I termini tecnici trovati nel testo del sito, per tratto: si salvano
    questi, non il testo intero."""
    out = {}
    for key, _, _, rule in TRAITS:
        if rule.get("text"):
            found = sorted({m.group(0).lower() for m in re.finditer(rule["text"], text or "", re.I)})
            if found:
                out[key] = found[:6]
    return out


def signals(github_facts: dict | None, terms: dict[str, list[str]] | None = None) -> dict:
    """Quello che serve al confronto, preso dai dati gia' raccolti."""
    g = github_facts or {}
    text = " ".join([g.get("description") or "", " ".join(g.get("topics") or [])]).lower()
    return {
        "langs": {_norm(x) for x in g.get("languages_all") or g.get("languages") or []},
        "dirs": {_norm(x) for x in g.get("root_dirs") or []},
        "text": text,
        "terms": terms or {},
        "commits": int(g.get("total_commits") or 0),
    }


def similarity(sig: dict) -> dict:
    """{score 0-100, traits: [{key, label, weight, got}]}"""
    out, total = [], 0.0
    for key, label, weight, rule in TRAITS:
        if key == "depth":
            n = sig.get("commits") or 0
            got = 0.0 if n < DEPTH_MIN else min(1.0, (n - DEPTH_MIN) / (DEPTH_FULL - DEPTH_MIN) * 0.7 + 0.3)
        else:
            hits = 0
            hits += bool(rule.get("langs") and sig["langs"] & rule["langs"])
            hits += bool(rule.get("dirs") and sig["dirs"] & rule["dirs"])
            found = set(sig.get("terms", {}).get(key) or [])
            if rule.get("text"):
                found |= {m.group(0).lower() for m in re.finditer(rule["text"], sig.get("text") or "", re.I)}
            hits += min(len(found), 2)
            # Due indizi indipendenti (per esempio il linguaggio e il testo) valgono il tratto intero.
            got = min(1.0, hits / (2 if rule.get("text") else 1))
        total += weight * got
        out.append({"key": key, "label": label, "weight": weight, "got": round(got, 2)})
    return {"score": round(total), "traits": out}


def is_benchmark(chain: str, token: str) -> bool:
    return chain == CHAIN and token == TOKEN


def seed_item() -> dict:
    """DREGG come voce della classifica, anche prima che il radar lo porti."""
    return {"chain": CHAIN, "token_address": TOKEN, "symbol": NAME, "name": "Dragon's Egg",
            "website": WEBSITE, "github": GITHUB, "narratives_hint": ["zero_knowledge"]}
