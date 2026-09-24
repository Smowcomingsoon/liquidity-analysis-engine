"""
Segnalazioni dei problemi, scritte dall'app.

Chi scrive cosa è separato di proposito, come per i file nel repo:

  * Le SEGNALAZIONI le scrivete voi dall'interfaccia. Vivono nel volume Fly, in
    `data/issues.json`, e sopravvivono ai deploy.
  * Le RISOLUZIONI le scrive Claude, in `app/data/resolutions.json`, che è
    versionato in git e viaggia con il deploy.

Nessuno dei due lati può quindi sovrascrivere l'altro, e ogni risoluzione porta
con sé la storia del commit che l'ha introdotta invece di un campo "data" che
qualcuno deve ricordarsi di compilare.

L'unione avviene in lettura, agganciando la risoluzione all'id della
segnalazione: gli id sono generati dal server e restano stabili, quindi il
riferimento regge anche se l'elenco viene riordinato o filtrato.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Literal

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_STORE_FILE = _DATA_DIR / "issues.json"
# Le risposte seguono la stessa divisione delle segnalazioni: quelle scritte
# dall'app vivono sul volume e appartengono a chi le scrive, quelle di Claude
# arrivano col deploy. Un botta e risposta in cui una parte puo' riscrivere le
# parole dell'altra non e' una conversazione.
_REPLIES_FILE = _DATA_DIR / "issue_replies.json"
# Quali voci della lista iniziale sono gia' state inserite. Serve a distinguere
# "mai inserita" da "inserita e poi cancellata": senza questo file l'unico modo
# per non resuscitare una voce cancellata era non inserirne mai di nuove.
_SEEDED_FILE = _DATA_DIR / "seeded_ids.json"

# Versionato in git e distribuito col deploy: è il lato di Claude.
_RESOLUTIONS_FILE = Path(__file__).resolve().parent / "data" / "resolutions.json"
_CLAUDE_REPLIES_FILE = Path(__file__).resolve().parent / "data" / "replies.json"

_MAX_ISSUES = 500

Status = Literal["aperto", "in corso", "risolto", "scartato"]
Priority = Literal["alta", "media", "bassa"]

_VALID_STATUS = {"aperto", "in corso", "risolto", "scartato", "riaperto"}
_VALID_PRIORITY = {"alta", "media", "bassa"}

_issues: list[dict] = []
_seeded_ids: set[str] = set()
_replies: list[dict] = []


def _load() -> None:
    if not _STORE_FILE.exists():
        return
    try:
        raw = json.loads(_STORE_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            _issues.clear()
            _issues.extend(raw)
    except (json.JSONDecodeError, OSError):
        pass


def _save() -> None:
    try:
        _STORE_FILE.write_text(json.dumps(_issues, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _load_replies() -> None:
    if not _REPLIES_FILE.exists():
        return
    try:
        raw = json.loads(_REPLIES_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            _replies.clear()
            _replies.extend(x for x in raw if isinstance(x, dict))
    except (json.JSONDecodeError, OSError):
        pass


def _save_replies() -> None:
    try:
        _REPLIES_FILE.write_text(json.dumps(_replies, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _claude_replies() -> dict[str, list[dict]]:
    """Le risposte di Claude, rilette a ogni richiesta come le risoluzioni."""
    try:
        raw = json.loads(_CLAUDE_REPLIES_FILE.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def add_reply(issue_id: str, text: str, author: str = "", reopen: bool = False) -> dict:
    """Una risposta a una segnalazione, con la possibilita' di riaprirla.

    Riaprire serve perche' "risolto" lo decide chi ha scritto la segnalazione,
    non chi ha scritto la correzione: se il problema si vede ancora, la voce
    deve tornare in cima all'elenco invece di restare chiusa per decreto.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("Risposta vuota")
    entry = {
        "id": uuid.uuid4().hex[:8],
        "issue_id": issue_id,
        "text": text[:4000],
        "author": (author or "").strip()[:120],
        "created_at": int(time.time()),
        "reopen": bool(reopen),
    }
    _replies.append(entry)
    _save_replies()
    if reopen:
        for it in _issues:
            if it["id"] == issue_id:
                it["reopened_at"] = entry["created_at"]
                _save()
                break
    return entry


def _load_seeded() -> None:
    if not _SEEDED_FILE.exists():
        return
    try:
        raw = json.loads(_SEEDED_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            _seeded_ids.update(str(x) for x in raw)
    except (json.JSONDecodeError, OSError):
        pass


def _save_seeded() -> None:
    try:
        _SEEDED_FILE.write_text(json.dumps(sorted(_seeded_ids)), encoding="utf-8")
    except OSError:
        pass


def _resolutions() -> dict[str, dict]:
    """Le risoluzioni scritte da Claude, lette a ogni richiesta.

    Rilette ogni volta anziché memorizzate all'avvio: il file arriva col deploy,
    e un processo che sopravvive a più deploy servirebbe altrimenti le
    risoluzioni della versione con cui è partito.
    """
    try:
        raw = json.loads(_RESOLUTIONS_FILE.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


_SEED = [
    ("seed-0115", "Sito senza nomi personali, con due sezioni distinte: gemme e meme",
     "Richiesta: 'rendilo privacy centrico, nel senso che non ci deve essere scritto Lorenzo [...] ci deve essere una sezione che riguarda il trading di meme coin [...] mentre per quanto riguarda i progetti seri voglio che anche li' ci sia uno scan H24 [...] che poi finisce nella leaderboard [...] anche a livello di interfaccia grafica siano distinte queste due sezioni prima di poter entrare'. Poi: 'deve essere anche chiaro che e' un qualcosa di estremamente gambling, la sezione per le meme coin' e 'revisione a tutto il sito in ottica degen [...] i dettagli che non serve a nessuno sapere non ci devono essere'.",
     "alta"),
    ("seed-0110", "La memoria deve studiare i token scartati che poi sono esplosi, e le meme vanno cavalcate",
     "Lorenzo: 'ho visto che hai scartato dei token che hanno fatto grandissime percentuali, per cui devi andare a studiare tutti quei casi li' e fare in modo che la prossima volta quei progetti vengano fuori con le stesse caratteristiche. Dobbiamo cavalcare le meme coin sostanzialmente, per cui deve essere anche chiaro che e' un qualcosa di estremamente gambling'.",
     "alta"),
    ("seed-0109", "Pagina di presentazione del progetto e del token da lanciare",
     "Lorenzo: 'fai una pagina web di presentazione del progetto con slogan accattivante in inglese; crea il ticker del token, ci serve lanciarlo e inventati anche una tokenomics [...] mi piace lo stile dell'app (che dovra' essere un tasto al centro dove \"ENTER THE APP\") [...] facciamo chiaramente capire che ci sono due sezioni; una per sniperare progetti nuovi con regole (facendole vedere) un'altra sezione per progetti (seri con fondamentali) ma sempre microcap [...] devi pensare a tutto anche a cose che non ti ho detto'.",
     "alta"),
    ("seed-0108", "Il radar deve imparare da cosa succede davvero ai token che propone e scarta",
     "Lorenzo: 'dobbiamo implentare una specie di \"coscienza\", imparare dai segnali; da cosa effettivamente succede, e migliorare il sistema'.",
     "alta"),
    ("seed-0107", "Rug pull su VISE, token proposto: -93%",
     "Lorenzo: 'abbiamo appena subito un rugpull su VISE che era stata proposta, il token ha fatto -93%, impara da questo errore. Cosa non avevamo visto?'",
     "alta"),
    ("seed-0106", "Radar: DREGG deve uscire davvero, e cercare progetti tecnici anche oltre i token freschi",
     "Lorenzo: 'voglio che drag tu mi verifichi che esca con queste regole, perche' deve uscire [...] estrai punti chiave da questo progetto e applica come regola [...] per altri progetti che possono essere simili dal punto di vista tecnologico [...] devi ampliare le ricerche [...] scandagliare tutto compreso anche X [...] non ci limitiamo a token freschi [...] anche un anno puo' essere giusto'.",
     "alta"),
    ("seed-0105", "Wallet on-chain: troppi menu e tasti, voglio un'esperienza da DEX",
     "Lorenzo: 'voglio che l'esperienza su questo sito sia paragonabile a un DEX [...] devi rendere l'app migliore, esaminarla nel suo complesso, aggiungere funzioni, perche' e' molto carina adesso, pero' ci sono tanti menu, tanti tasti che potrebbero essere secondo me ridotti'.",
     "media"),
    ("seed-0104", "Classifica dei progetti migliori, con analisi profonda di GitHub, sito e tokenomics",
     "Lorenzo: 'devi fare una specie di leaderboard che è sostanzialmente una classifica [...] cose estetiche che facciano capire primo posto, secondo posto, terzo posto e deve essere una classifica dinamica [...] aggiornata in tempo reale [...] rigoroso [...] da GitHub E devi controllare i commit insomma deve essere profonda sta cosa la tokenomics non deve essere uno scam il sito deve essere fatto bene magari se riesci ad analizzarne l'html e vedere se è fatto bene o se è solo una pagina di presentazione ma dietro non c'è niente [...] segnalare anche da quanto è che sono usciti [...] e ampliamo anche la chain per i token lanciati su Pump Fun'.",
     "alta"),
    ("seed-0103", "Telegram: i messaggi arrivano tagliati, non sono mai completi",
     "Lorenzo: 'Inoltre i messaggi su Telegram arrivano tagliati, non sono mai completi.'",
     "alta"),
    ("seed-0102", "Radar: DREGG escluso perche' ha piu' di 3 settimane e supera i $2M",
     "Lorenzo: 'con questa regola dovresti trovare $DREGG, vediamo se sei bravo'. Poi: 'dregg va bene anche se ha 21 gg e supera i 2 mln, progetti in quel modo voglio che siano eccezione in quanto tecnicamente incredibili su github'.",
     "alta"),
    ("seed-0101", "Wallet on-chain: nello swap non si capisce cosa ho, e scegliere un token fa scendere la pagina",
     "Lorenzo: 'dovrebbe automaticamente selezionarmi la valuta che ho nel wallet. Io ho fatto degli swap da Ethereum a USDG, ma non e' chiaro che moneta io abbia nel portafoglio. [...] Deve essere selezionata automaticamente la valuta detenuta, quella con maggior quantita'. Poi quando seleziono una valuta per fare lo swap la schermata mi va in basso e mi tocca tornare su'.",
     "alta"),
    ("seed-0100", "Radar: cap fino a $2M per le gemme, e token scelti e scartati spariti dopo un aggiornamento",
     "Lorenzo: 'alziamo un po' il limite di market cap anche perché il mio obiettivo è trovare delle gemme per cui anche uno o due milioni di market cap potrebbe andare bene però nel momento in cui dal punto di vista GitHub, sito, tokenomics tante cose devono essere perfette [...] adesso ho refreshato il sito e non ho più l'elenco [...] dei token scartati e il token che prima era stato scelto non lo vedo più fa in modo che ogni volta che viene scelto un token venga salvato in database così che io ad ogni accesso possa rivedere anche dopo un riavvio'.",
     "alta"),
    ("seed-0099", "Radar: ingressi non nel punto preciso del grafico, e niente short",
     "Lorenzo: 'se seleziono una strategia non appare nel grafico gli ingressi in modo preciso'. E: 'non voglio short, solo acquisti spot e vendite'.",
     "alta"),
    ("seed-0098", "Strategia Lorenzo ad alta frequenza per i token on-chain appena usciti",
     "Lorenzo: 'creare un algoritmo di trading adatto per l'alta frequenza per cavalcare queste [...] cripto [...] e non perdersi [...] i rialzi. Non voglio utilizzare strategie inverse perché credo che qui quando il prezzo sale sale [...] il trend follower è corretto però voglio che lo ottimizzi per questa particolare applicazione on chain [...] voglio comunque mantenere la logica di quella di Lorenzo però applicata ad un time frame [...] sui minuti, quindi con dei filtri diversi'.",
     "alta"),
    ("seed-0097", "Radar: grafico vuoto con DexScreener pieno, token oltre i $100k, scartati e pagella dei filtri",
     "Lorenzo: 'Se Dex Scanner visualizza correttamente il ticker con tutta la liquidità, eccetera, nel sito nostro non può uscire questo errore inaccettabile, non deve mai succedere' (FLIP su Robinhood: 'Nessun dato disponibile'). E: 'voglio capire che è una cosa live [...] deve esserci un senso di movimento', 'se ci clicco voglio vedere tutti i parametri di GitHub [...] un bel benchmark [...] se in modo eccellente oppure [...] mediocri', 'invece di usare Token Sniffer usa qualche sito che ha delle api pubbliche', 'scorrendo giù voglio una sezione che mi faccia vedere tutti quelli che sono stati scartati'. Più l'avviso Telegram '⚠️ Dati stantii: UBR@solana [...] [1m] — ultima candela 0h fa'.",
     "alta"),
    ("seed-0096", "Condividere il sito con un amico in sola lettura",
     "Lorenzo: 'vorrei pubblicare un link per condividere questo sito che abbiamo fatto con un amico che lo possa utilizzare senza ovviamente poter modificare niente, quindi solo la lettura. È possibile mostrarglielo se copio il link del sito cosa gli arriva?'",
     "media"),
    ("seed-0095", "Radar: solo token legittimi sotto i $100k, con sito, GitHub e Token Sniffer, e il radar come prima pagina",
     "Lorenzo: 'voglio evitare rug pull, voglio evitare puttanate, stronzate, voglio token legittimi con progetti seri [...] una market cap non oltre i 100.000 dollari [...] voglio avere anche la possibilità con un link di cliccare e andare sul sito del token [...] se si riesce a fare anche questo controllo con Claude sarebbe ideale'. Poi: 'fai un controllo anche per github [...] voglio evitare SHITCOIN e MEMECOIN', 'aggiungi anche il report di token sniffer [...] che ci fa da filtro ulteriore', e 'vorrei che la prima pagina quando entro sia proprio questa qui [...] i pop-up che vanno in una lista e rimangono [...] e il grafico [...] di tutti quelli rilevati quale strategia performerebbe meglio facendo un backtest, in tempo reale con gli ingressi'.",
     "alta"),
    ("seed-0094", "Wallet on-chain: un alert costante sui token giovani, provati su tutte le strategie",
     "Lorenzo: 'voglio che nell'applicazione venga informato di quale token appena nato, o comunque che ha due o tre settimane di vita con tanta liquidità e bassa capitalizzazione [...] quale delle strategie performa bene, una specie di alert costante che scansionando il mercato mi propone [...] c'è questo ticker su Solana o su rete Robinhood che performerebbe bene con questo tipo di strategia [...] deve esserci un minimo di backtest'. Poi: 'per il grafico [...] voglio la stessa grafica del sito [...] zoomare [...] vedere in tempo reale gli ingressi e le uscite delle strategie se le avessi attivate [...] strategie che eventualmente potrei attivare oppure no'.",
     "alta"),
    ("seed-0093", "Wallet on-chain: il saldo si vede in una sola unità",
     "Lorenzo: 'in questo momento io visualizzo il saldo in Ethereum, voglio che se clicco su ETH mi switchi la visualizzazione in dollari, in Bitcoin e basta'.",
     "bassa"),
    ("seed-0092", "Wallet on-chain: cercando MYCEL esce MYCE, poi grafico vuoto e swap bloccato",
     "Lorenzo, da iPhone su Robinhood Chain: 'sto errore non deve MAI SUCCEDERE'. Aveva cercato MYCEL: la ricerca lo scartava (pool da $4.7k sotto la soglia) e restava MYCE; il grafico mostrava 'Nessun pool per questo token su nessun fee tier' e 'Nessun dato disponibile per MYCE@robinhood:0x...', lo swap 'Preventivo non disponibile'. Poi: 'non e' che deve escludermi dei risultati [...] io ti scrivo tutta roba che e' presente su Dex Scanner'.",
     "alta"),
    ("seed-0091", "Wallet da telefono: pagina decentrata all'apertura, rete da cambiare nel menu, nessun avvio",
     "Lorenzo, da iPhone: 'quando entro nel sito da mobile fai in modo che tutto sia perfettamente allineato al monitor, adesso appena lo apro e' decentrato, voglio un'animazione anche all'avvio appena mi collego [...] con scritto DEGEN'. E: 'per cambiare rete voglio che ci sia un simbolino [...] che mi faccia capire che posso cambiare rete con un click, senza andare a pigiare il tasto nel menu'.",
     "media"),
    ("seed-0090", "Wallet on-chain: molti ticker non si trovano, e per ogni ticker escono doppioni",
     "Lorenzo: 'mezzi ticker non funzionano; voglio solo un unico risultato per ticker, quello con il contratto corretto e verificato con liquidità migliore, non voglio dover scegliere altri doppioni'.",
     "media"),
    ("seed-0089", "Wallet on-chain: troppi tasti, e con la strategia accesa non si vedeva cosa stava facendo",
     "Lorenzo: 'voglio potermi fidare di questa applicazione visto che quando attivo la strategia utilizzo soldi reali, voglio avere il completo controllo di quello che sta succedendo [...] mi crea confusione un'interfaccia con troppi tasti [...] odio i tasti [...] tanta diagnosi nel momento in cui una strategia e' attiva'. Prima ancora: 'l'app deve funzionare con pochi clic, pochi cambi di schermata' e 'rendi l'app da mobile un'esperienza SEMPLICISSIMA'. Concept 'Pilota' approvato: 'PROCEDI E' PERFETTO'.",
     "alta"),
    ("seed-0088", "Wallet on-chain da telefono: scegliere un token apre troppe schermate",
     "Lorenzo: 'rivedi completamente il sito se mi collego da mobile; se seleziono un token si aprono troppe schermate'. Su /onchain-app.html da telefono il token scelto sul grafico non arrivava allo swap: andava riscelto nel pannello ordine, che stava sotto l'elenco delle posizioni, con un secondo foglio e la tastiera aperta in automatico.",
     "media"),
    ("seed-0087", "Pagina wallet on-chain lenta ad aprirsi",
     "Lorenzo: 'usa un linguaggio di programmazione per rendere il sito velocissimo a costo di fare un refactory'. Misurata onchain-app.html dal vivo: Lighthouse 99 da non loggati, main thread 0,4 s, JS leggero. Il tempo se ne andava in giri di rete in fila prima dei dati (HTML, main.js, poi i suoi import, poi /api/auth/me, poi /api/onchain/wallets, solo dopo il saldo), piu' i font Google che bloccavano il primo disegno e la libreria del grafico senza cache.",
     "media"),
    ("seed-0086", "Wallet on-chain: i tasti Strategie, Attività e Impostazioni in alto non rispondono",
     "Segnalato dal progetto 'Onchain app performance e pulsanti': su /onchain-app.html la barra in alto mostra Wallet / Strategie / Attività / Impostazioni, ma cliccare le ultime tre non fa nulla.",
     "media"),
    ("seed-0085", "Fase 1 del trading automatico on-chain: wallet generato sul server, sola lettura",
     "Lorenzo: 'si potrebbe iniziare ad imbastire il sistema per il trading automatico anche su wallet on-chain'. Chiarito con lui: custodia della chiave sul server (non un servizio esterno), nessuna conferma manuale per operazione — l'automazione deve dipendere solo dalla strategia — e wallet 'privacy driven' nel senso di generato da zero da noi, mai legato a un'identita', non tecniche di offuscamento on-chain. Chain scelte: Solana e Robinhood Chain (L2 Arbitrum Orbit di Robinhood, live da luglio 2026, chain id 4663). Questa e' solo la Fase 1 del piano (generazione chiave + saldo, nessuna firma reale) — le fasi successive (simulazione, poi esecuzione vera con tetti/allowlist) restano da fare, apposta.",
     "media"),
    ("seed-0084", "Ticker DEX (on-chain) nel ticker del sito principale, non solo nello Sniper",
     "Lorenzo: 'riusciamo ad ottenere anche i ticker di dex scanner?' — chiarito: non dentro lo Sniper (gia' collegato li'), ma nel campo ticker del grafico/backtest principale. Ricerca per nome/simbolo come i pair CEX, su tutte le chain gia' coperte dallo Sniper.",
     "media"),
    ("seed-0083", "Voce sidebar \"Libreria\" tolta, ridondante col pulsante Indicatori in toolbar",
     "Lorenzo: 'elimina la tab libreria, tanto abbiamo già indicatori in alto'. Il modale della libreria aveva tre punti d'ingresso (voce sidebar, pulsante nella toolbar, pulsante dentro Livelli) — tolta la voce sidebar, che duplicava il pulsante toolbar senza motivo. Restano il pulsante in toolbar (l'ingresso principale) e quello dentro Livelli (contestuale, per chi ha gia' aperto quel pannello).",
     "bassa"),
    ("seed-0082", "Indicatori della libreria: rimozione al passaggio del mouse, nome a grafico, modale con preferiti",
     "Tre richieste di Lorenzo nella stessa serata sullo stesso filone. Prima: 'quando aggiungo indicatori dalla libreria voglio poterli togliere cliccandoci sopra al grafico e rimuovendoli' — poi affinata: 'la rimozione deve avvenire tramite un flag che appare con una x se passo il cursore sugli indicatori, qualunque essi siano a grafico', per non rimuovere per sbaglio con un click diretto. Poi: 'voglio vedere il nome degli indicatori a grafico una volta attivati; voglio poterci entrare e capire come ragionano, ora se li attivo finiscono nel grafico ma non so cosa sono e cosa ho attivo'. Infine, con uno screenshot di TradingView: 'riesci a copiare l'interfaccia [...] PRECISAMENTE UGUALE?' — spiegato che il design esatto di un prodotto di terzi non e' riproducibile (non e' un limite tecnico), ma la struttura funzionale si': modale centrale con colonna categorie e Preferiti invece del disegno esatto.",
     "media"),
    ("seed-0081", "Il grafico apriva già affollato di livelli, per chi li aveva accesi una volta",
     "Lorenzo, con uno screenshot pieno di etichette sovrapposte (pattern, segnali contestuali, MACD, Stocastico): 'sta visualizzazione non ha senso vederla; di predefinito fai che non ci sia nessun indicatore acceso'. Nel codice i default erano gia' quasi tutti spenti (solo EMA era true) — il resto arrivava dalle preferenze salvate sull'account, che ricordano l'ultima combinazione di livelli accesa e la ripropongono ad ogni accesso. Cambiato l'ultimo default rimasto (EMA da acceso a spento): un account nuovo, senza preferenze salvate, parte ora con il solo prezzo. Verificato dal vivo registrando un account pulito. Non risolve da solo la vista di un account che ha gia' salvato dei livelli accesi in passato — quelli restano finche' non li si spegne una volta dai toggle in Livelli, dopodiche' restano spenti.",
     "media"),
    ("seed-0080", "Libreria indicatori: da tendina nascosta a pannello sfogliabile con spiegazioni",
     "Lorenzo, dopo la prima versione (seed-0079): 'voglio una libreria dove visualizzo tutto con spiegazioni, cosi capisco cosa attivare, non conosco i nomi cosi non so cosa cercare'. Scritta una descrizione dedicata per ciascuno dei 151 indicatori (cosa misura, in una riga, non solo il nome tecnico). Poi, guardando lo spazio vuoto nella toolbar in alto: 'sfruttiamo questo spazio evidenziato in giallo, per inserisci un po' di cosine tra cui questa libreria'. Rifatta l'interfaccia: non piu' una tendina che si nasconde finche' non cerchi, ma un pannello ampio (stessa famiglia di Bacheca/Script Pine) sfogliabile per categoria, con due punti d'ingresso nuovi — un pulsante nello spazio vuoto della toolbar e una voce propria in sidebar ('Libreria') — oltre al collegamento gia' esistente dentro Livelli. Un bug trovato verificando dal vivo con Playwright: il pulsante nella toolbar e quello dentro Livelli aprivano il pannello e lo richiudevano nello stesso click, perche' il click originale (non quello sintetico generato per aprire il pannello) risaliva fino al listener globale che chiude i pannelli su un click 'fuori dalla sidebar' — quel listener non riconosceva ne' il bottone ne' il pannello appena aperto come 'dentro'. Risolto con stopPropagation sui due bottoni.",
     "media"),
    ("seed-0079", "Libreria indicatori community (pandas-ta) collegata, con tendina cercabile",
     "Lorenzo aveva chiesto se si potesse accedere a indicatori open source fatti dalla community, come su TradingView. Gli script Pine reali non sono portabili (l'interprete custom del sito copre solo un sottoinsieme del linguaggio), ma pandas-ta era gia' una dipendenza installata e ne usava solo 6 dei 151 disponibili. Collegati tutti e 151, dietro una tendina cercabile in Livelli & Indicatori — 'non voglio affollamento nel grafico, servira' una libreria a tendina da dove selezionarli' — cosi' nessuno si calcola finche' non viene scelto esplicitamente. Ogni indicatore e' classificato overlay (va sul prezzo) o oscillator (apre un riquadro proprio, creato e distrutto dinamicamente). Verificato dal vivo con Playwright: aggiunta/rimozione crea e distrugge il riquadro senza lasciare residui.",
     "media"),
    ("seed-0078", "Idea: arricchire i profili Sniper con API pubbliche (TVL, revenue, audit)",
     "Lorenzo: 'introduciamo anche altri sistemi per scansionare al meglio il mercato, laddove sia disponibile qualche API online pubblica'. Candidati: CoinGecko (cap, volume), DeFiLlama (TVL, revenue per chain), Messari (developer activity), Token Terminal (protocol revenue), API nativa Robinhood Chain se esiste. Non prioritario: sbloccare dopo che lo Sniper e' stabile su Robinhood Chain.",
     "bassa"),
    ("seed-0077", "Alert Telegram: il prezzo mostrato era quello della candela del segnale, senza dire quando",
     "Lorenzo: 'ho ricevuto questo avviso dal bot: INJUSDT [1D] Ingresso long rilevato, Prezzo: 6.285 — ma il prezzo attuale e' 5,43$... inoltre mi sembra strano l'ingresso long dopo una correzione'. Il 6.285 era la chiusura del 14 settembre, consegnata il 16 dopo un -13,5%: il segnale era nato PRIMA della correzione, non dentro. Il messaggio pero' mostrava solo quel prezzo, senza data, quindi un segnale di due giorni prima si leggeva come uno di adesso.",
     "alta"),
    ("seed-0076", "Anteprima ordine futures: prometteva l'importo chiesto, l'ordine ne usava meno",
     "Dal socio di Lorenzo: 'non ha usato la giusta percentuale del portafoglio... 80%, quindi doveva essere 40 dollari ma ne ha usati 34 e qualcosa, come se avesse ordinato con il contract'. Intuizione giusta: su MEXC la dimensione si conta in contratti interi, e un contratto su KAS vale 1000 monete (~34,40 USDT). 40 USDT sono 1,16 contratti, troncati a 1. L'ordine era corretto; a mentire era l'anteprima, che mostrava i 40 chiesti.",
     "media"),
    ("seed-0075", "Nessun modo di chiudere una posizione dal sito, ne' spot ne' futures",
     "Dal socio di Lorenzo, con una posizione MEXC aperta: 'Ma l'opzione per chiudere la posizione non c'e'?'. Le posizioni erano sola lettura e non esisteva nemmeno l'endpoint: close_position c'era, ma lo raggiungeva solo il trading automatico sui segnali di uscita. Chi apriva dal sito poteva solo guardare, e per uscire doveva passare dall'app dell'exchange. Lorenzo: 'la chiusura deve esserci anche per lo spot'.",
     "alta"),
    ("seed-0074", "Un ordine futures riuscito veniva mostrato come 'Errore di rete'",
     "Lorenzo: 'Errore di rete: Can't find variable: loadJournal'. Il messaggio arrivava dal ramo del SUCCESSO: dopo un ordine eseguito, la chiamata a una funzione inesistente (loadJournal, il nome vero e' loadTradingJournal) lanciava un ReferenceError che il catch riscriveva come errore di rete. L'ordine era passato davvero, sullo stesso schermo da cui si rischiava di rifarlo una seconda volta.",
     "alta"),
    ("seed-0073", "Il sito sembra 'AI slop': testo minuscolo, bordi/ombre da template generico",
     "Lorenzo: 'tutto il sito sembra fatto da ai slop'. Scansione con lo strumento impeccable (61 regole deterministiche): 145 problemi, concentrati in pochi pattern sistemici (testo sotto gli 11px funzionali/12px di corpo, bordo colorato spesso su un lato delle card, bordo sottile+ombra larga).",
     "media"),
    ("seed-0072", "Ordine futures MEXC bloccato da un firewall/CDN, letto erroneamente come permesso mancante",
     "Lorenzo: ordine manuale con 50 USDT rifiutato con una pagina HTML 'Access Denied' di Akamai — il codice la leggeva come errore di chiave. Causa probabile: IP europeo del server negato da MEXC su /order/submit per i derivati.",
     "media"),
    ("seed-0071", "Trading automatico: nessun tetto per strategia, e % saldo non funzionava su MEXC",
     "Lorenzo (voce): 'io magari voglio fare 10 ordini da 500 dollari l'uno con il bot, ma se lui pesca con la percentuale e' finita' — la credenziale exchange (size_mode/size_value) e' UNA sola, condivisa da tutte le strategie automatiche che ci operano sopra, quindi non basta a limitare quanto rischia ciascuna. Separatamente: 'voglio usare la % del saldo per gli ordini automatici... pero' sembra non funzionare su mexc'.",
     "media"),
    ("seed-0070", "Ordine futures manuale: nessun modo di piazzare una quantita' di token diretta senza rischiare di confondere USDT e unita'",
     "Lorenzo: 'si rischia dei disastri se uno puo' [inserire] migliaia di dollari dentro e se va a pescare in maniera sbagliata e te li seleziona invece come dollari, come unita' di misura token diventa un delirio... stavo pensando alla possibilita' di fargli fare dall'app direttamente in token, ma e' un casino perche' poi hai un'unita' di misura fissa per tutti... bisognerebbe provare a fargli switchare a lui'.",
     "media"),
    ("seed-0069", "Modalita' Semplice: nessun riferimento al timeframe nello step Backtest/Ottimizza",
     "Lorenzo: 'non trovo il riferimento del timeframe...' — lo step Backtest mostrava solo il ticker, senza dire su quale timeframe la strategia stava lavorando.",
     "bassa"),
    ("seed-0068", "Modalita' Semplice: da archivio il grafico chiede il ticker di nuovo, e nessun collegamento fra un ingresso e l'operazione",
     "Lorenzo: 'se io seleziono una strategia dall'archivio, il ticker e' sottinteso, per cui tu quando carichi il grafico... non e' che devo riselezionare il ticker, vedi che mi chiede il ticker di nuovo? Tu mi devi far vedere il grafico aggiornato per quella strategia con quegli ingressi precisi... e ogni ingresso se ci clicco mi deve rimandare alla transazione effettuata dalla strategia'.",
     "alta"),
    ("seed-0067", "Modalita' Semplice: l'ottimizzazione finisce in errore, log illeggibile, nessun modo di fermarla",
     "Lorenzo: 'la strategia di ottimizzazione da errori alla fine e quindi non funziona, pero c'e' anche questa casella chiamata object che non si sa che cosa vuol dire. Eredita tutta la logica anche grafica che abbiamo creato nel sito per ottimizzazione stabile... Permettimi anche di fermarla se voglio fermarla, perche' vorrei poter cambiare idea'.",
     "alta"),
    ("seed-0066", "Modalita' Semplice: selezionare un ticker sembra non fare niente finche' non arriva il grafico",
     "Lorenzo: 'quando seleziono il ticket il click non produce un'accettazione grafica, ma sembra che non carichi anche se in realta' il ticket e' stato selezionato e poi il grafico arriva... voglio un effetto quando seleziono il ticket che mi faccia capire che sia selezionato'.",
     "bassa"),
    ("seed-0065", "Modalita' Semplice: impossibile testare una chiave exchange senza perderla, e timeframe incoerente fra backtest/ottimizzazione/attivazione",
     "Lorenzo, dopo seed-0064: 'voglio poterle testare, voglio poter capire se queste api funzionano subito... non voglio che siano non salvate. Voglio che il processo sia quello ufficiale, però con un'opzione in più... vuoi testarle subito? ... mi permetti di visualizzare il saldo... e di poter fare un ordine manuale... perché voglio che tu capisca proprio perché tante volte che ti chiedo di sistemare questa cosa dei ticket dello spot e del futures che voglio che non accada più'. Anche: 'devo già aver selezionato i timeframe sulla quale voglio che la strategia lavori' e il riepilogo finale deve mostrarlo.",
     "media"),
    ("seed-0064", "Modalita' Semplice: nessun archivio, grafico non reale, pochi exchange, chiavi da rifare ogni volta",
     "Lorenzo, dopo seed-0062/63: 'vorrei anche che quando sempre in modalita' semplice sia permesso di selezionare la strategia... accedere all'archivio... vedendo tutti i dettagli... esportarli in Excel'; 'il grafico che tu mi mostri nella versione semplificata... deve essere dello stesso tipo di quello raffigurato nel sito originario, le nostre grafiche, non un altro grafico'; 'una volta che uno inserito le API devono sparire... non deve esserci una tab piena di caselle da riempire'; 'ci deve essere tutti gli exchange, non soltanto per i tre'.",
     "media"),
    ("seed-0063", "Modalita' Semplice visibile anche da desktop",
     "Il bottone 'Modalita' Semplice' compariva nella toolbar desktop, non solo nel menu mobile 'Altro' — ma e' pensata solo per il telefono, su desktop il resto dell'app e' gia' raggiungibile per intero.",
     "bassa"),
    ("seed-0062", "Nessun modo di testare una chiave API o vedere il saldo prima di operare",
     "Lorenzo: 'vorrei poter effettuare dei test preliminari di collegamento all'exchange... vorrei anche che fosse visualizzato il saldo una volta collegate le API, prima di poter fare poi le vere operazioni... vorrei poter fare anche operazioni manuali, quindi provare proprio a fare il test'. Chiesta esplicitamente una sezione isolata ('circoscritta'), cosi' un problema li' dentro si sistema in poco tempo senza toccare il resto.",
     "media"),
    ("seed-0061", "Un CDN irraggiungibile (grafico o Script Pine) bloccava silenziosamente tutto il sito, ricerca pair compresa",
     "index.html caricava Lightweight Charts e CodeMirror da unpkg.com; se uno dei due non rispondeva, un'eccezione non gestita nella sequenza di avvio impediva a ogni altro bind (ricerca ticker in Lab, ordini spot/futures, navigazione) di partire — trovato investigando le segnalazioni 54cbf520 e 2a7fd905 sui pair introvabili.",
     "alta"),
    ("seed-0060", "Il grafico cambia dimensione/posizione passando da una tab all'altra",
     "In Analizza il grafico prendeva tutto lo spazio libero, nelle altre aree (Lab, Watchlist, Sorveglia, Livelli) si restringeva a una colonna fissa — il layout 'si scompigliava' a ogni cambio tab invece di restare stabile con solo il pannello laterale che si adegua.",
     "media"),
    ("seed-0059", "Kraken senza aggiornamento in tempo reale, solo ogni 60s",
     "Solo MEXC aveva un feed live via websocket; Binance, KuCoin e Kraken restavano a polling REST ogni 60 secondi (badge 'Agg. ogni 60s'). Kraken ha un feed pubblico websocket v2 gratuito, mai collegato.",
     "media"),
    ("seed-0058", "Kraken: 'Timeframe 1M non supportato' selezionando 1d",
     "Selezionare 1d come intervallo principale sceglie automaticamente 1M come HTF (contesto di lungo periodo) per l'analisi multi-timeframe — Kraken non ha un intervallo mensile nativo nella sua API pubblica e il provider lo segnalava come errore invece di calcolarlo dalle candele giornaliere.",
     "media"),
    ("seed-0057", "Attivare il trading automatico su una strategia non era chiaro: bisognava passare da Opera e scegliere l'exchange fra troppi",
     "In Opera si sceglieva l'exchange fra una lista lunga prima di poter fare qualsiasi cosa, e il trading automatico era un interruttore unico per exchange che valeva per TUTTE le strategie attive li' sopra — nessun modo di dire 'questa strategia solo alert, quest'altra anche ordini reali'.",
     "media"),
    ("seed-0056", "Kraken non era fra gli exchange dati spot, solo un'opzione morta nei menu",
     "Kraken mette a disposizione API pubbliche complete per dati OHLC e ricerca simboli, ma non c'era nessun provider collegato: chi lo selezionava non trovava mai nessun ticker.",
     "bassa"),
    ("seed-0055", "La bacheca (/api/issues) chiedeva login, rompendo la lettura automatica a inizio sessione",
     "CLAUDE.md dice di controllare 'curl -s https://liquidityengine.app/api/issues' prima di ogni conversazione, ma il cancello di autenticazione globale (seed-0031) bloccava quella richiesta con un 401 silenzioso: sembrava 'nessuna segnalazione nuova' invece di 'non ci sono nemmeno arrivato'.",
     "alta"),
    ("seed-0054", "Ricerca Stabile troppo lenta, sceglie configurazioni poco robuste",
     "Gabriele: dopo ~10 minuti sembra bloccarsi su piu' ticker, e spesso sceglie una configurazione che regge solo 1-2 scosse su 14 senza che sia chiaro cosa significhi.",
     "alta"),
    ("seed-0053", "Nessun pair trovato su spot (Binance) e futures (MEXC)",
     "In Opera, scegliendo Binance il campo Pair non suggeriva mai nulla (il backend non interrogava quel provider); sui futures MEXC il campo Pair non aveva alcun autocomplete e un simbolo sconosciuto dava un errore 500 generico invece del messaggio previsto.",
     "alta"),
    ("seed-0052", "Ogni salvataggio da 500 - Permission denied sul volume", "Salvare credenziali, preferenze o preferiti dava Internal Server Error: il volume dati non era scrivibile dall'utente del container.", "alta"),
    ("seed-0051", "Nessun avviso Telegram al deploy", "Dopo un deploy su main non arriva piu' il messaggio Telegram che elenca le modifiche.", "media"),
    ("seed-0050", "Nessun modo di vedere gli errori 500 senza SSH", "Quando qualcosa si rompe davvero sul server, il traceback vive solo nei log del container: nessuno lo vede finche' non lo segnalo io.", "alta"),
    ("seed-0049", "Salvare le credenziali futures a volte non fa nulla", "Dopo un primo tentativo che fallisce, il pulsante Salva nel pannello futures smette di rispondere del tutto: nessun errore, nessuna richiesta.", "alta"),
    ("seed-0040", "Bottone leva sotto la soglia di contrasto", "Il bottone APRI LONG aveva un contrasto di 4,08:1, sotto la soglia AA di 4,5:1 richiesta per il testo.", "media"),
    ("seed-0041", "Pallino LIVE invisibile con animazioni ridotte", "Con 'riduci le animazioni' attivo nel sistema, il pallino che dice se il feed prezzi e' vivo restava fisso acceso.", "media"),
    ("seed-0042", "Prezzi in notazione scientifica nel testo di analisi", "Il riepilogo struttura scriveva prezzi tipo 7.672e+04 invece di un numero leggibile.", "media"),
    ("seed-0043", "Label dei form di trading senza collegamento accessibile", "I campi di Opera (Exchange, Pair, Leva, Passphrase) avevano etichette visive non collegate ai controlli.", "media"),
    ("seed-0044", "Testi e nomi vecchi rimasti dopo il ridisegno", "La tab-bar mobile e alcuni testi dicevano ancora 'Alert' invece di 'Sorveglia'.", "bassa"),
    ("seed-0045", "Watchlist vuota non spiega cosa si puo' fare da li'", "Lo stato vuoto della watchlist non menzionava che da li' si attivano anche alert e ordini automatici.", "bassa"),
    ("seed-0046", "Il gate di regime (VR/z/p/H) non si spiega da solo", "Le sigle del gate di regime comparivano senza spiegazione, mentre Sharpe e PBO nello stesso pannello si' — e VR da solo, senza il p-value, non basta a concludere niente.", "media"),
    ("seed-0047", "Chip Sharpe e Profit factor senza spiegazione", "I due numeri comparivano colorati ma senza dire cosa significano a chi non e' un quant.", "bassa"),
    ("seed-0048", "'% saldo' nell'ordine spot ordina alla cieca", "Scegliendo la taglia come percentuale del saldo, il saldo vero non si vedeva da nessuna parte.", "media"),
    ("seed-0038", "Ordine spot manuale senza conferma",
     "L'ordine spot in Opera partiva al primo click, senza anteprima ne' conferma, a differenza dei futures.",
     "alta"),
    ("seed-0039", "Analizza e Lab non sincronizzati al primo caricamento",
     "La sidebar mostra Analizza attivo ma il pannello a schermo e' quello del Lab, al primo avvio dell'app.",
     "media"),
    ("seed-0037", "Durante un deploy il sito mostra un errore grezzo",
     "Quando c'e' un deploy in corso vorrei che chi si collega veda un avviso invece di un errore, "
     "cosi' non sembra che il sito sia rotto.",
     "media"),
    ("seed-0036", "Il grafico non proietta il prezzo corrente sull'asse",
     "Serve la linea del prezzo attuale che arriva fino all'asse con l'etichetta, come su TradingView, "
     "su ogni grafico.",
     "media"),
    ("seed-0035", "\"Errore di rete\" sull'archivio, senza dire altro",
     "Premendo 'Attiva l'alert' su una voce d'archivio compare 'Errore di rete' e basta: non si capisce "
     "se il server non ha risposto o se ha risposto male.",
     "media"),
    ("seed-0034", "Lo sniper propone meme coin invece di progetti seri",
     "Voglio vedere solo crypto legittime, o quantomeno progetti seri: le meme coin non mi interessano.",
     "alta"),
    ("seed-0032", "Il tema non viaggiava davvero con l'account",
     "Il frontend mandava 'theme' a PUT /api/preferences ma l'allowlist del backend non lo includeva: "
     "il valore veniva scartato in silenzio e il tema restava legato al browser, non all'account.",
     "media"),
    ("seed-0031", "Il sito e' pubblico e le API rispondono a chiunque",
     "Con il trasloco su un dominio pubblico serve verificare che le funzioni pesanti e i dati "
     "non siano raggiungibili senza credenziali.", "alta"),
    ("seed-0033", "La sezione Watchlist non si trovava piu' da desktop",
     "Dopo il ridisegno (seed-0028), 'Sorveglia' nella sidebar apriva solo la tab Alert: la lista dei "
     "ticker rapidi (tab Watchlist, i preferiti da un clic) non aveva piu' nessuna voce di navigazione "
     "su desktop, solo dal menu mobile 'Altro'.", "alta"),
    ("seed-0028", "Ridisegnare il sito: ogni funzione al suo posto",
     "Il prodotto e' cresciuto per innesti e ogni funzione era disegnata bene da sola, ma nessuna "
     "parlava la lingua delle altre. Serve un occhio globale che rimetta in discussione dove sta "
     "ogni cosa, non solo che colore ha.", "alta"),
    ("seed-0029", "Il backtest non dice a che prezzo entra",
     "Non era possibile sapere se una candela viene considerata alla chiusura oppure nel momento in "
     "cui il segnale si verifica. Due rendimenti calcolati con ipotesi diverse non sono confrontabili, "
     "e l'ipotesi non era scritta da nessuna parte.", "alta"),
    ("seed-0030", "Le operazioni eseguite non lasciano traccia sul grafico",
     "Il grafico mostrava cosa la strategia diceva, mai cosa e' successo davvero: guardando la serie "
     "non si poteva rispondere alla domanda \"quella volta sono entrato o no?\". Ogni operazione, "
     "manuale o automatica, deve restare segnata.", "alta"),
    ("seed-0001", "Bug grafico sui trade del backtest", "", "media"),
    ("seed-0002", "Salvare il backtest in database quando si attiva un alert",
     "Una volta attivato l'alert di una strategia bisogna poter accedere al backtesting e salvarlo, "
     "per avere un archivio consultabile delle strategie generate dall'ottimizzazione.", "alta"),
    ("seed-0003", "Strategia Gabriele 2 con movimento short",
     "Da testare e applicare poi sui futures.", "media"),
    ("seed-0004", "Graficare l'imbalance FVG in stile TradingView",
     "Zone d'interesse a barre orizzontali.", "media"),
    ("seed-0005", "Valore RSI sul pallino blu invece che sulla freccia del cursore", "", "bassa"),
    ("seed-0006", "Pulsante per attivare/disattivare modalita' black", "", "bassa"),
    ("seed-0007", "Discrepanza nel numero di trade fra ottimizzazione e manuale",
     "Con gli stessi valori impostati a mano si ottengono risultati simili ma quantita' di trade molto "
     "diverse rispetto alla modalita' automatica. Ogni variabile aggiuntiva usata dalla logica automatica "
     "deve essere parametrizzata e visibile dopo l'ottimizzazione, per rendere il risultato replicabile "
     "a mano e verificabile.", "alta"),
    ("seed-0008", "Venti notifiche Telegram con prezzi disparati dopo aver attivato un alert", "", "alta"),
    ("seed-0009", "Filtri di ottimizzazione su trade in perdita e drawdown",
     "Poter selezionare solo strategie con un minimo/massimo di trade in perdita (compreso 0) "
     "o una percentuale di drawdown (compreso 0%).", "media"),
    ("seed-0010", "Dubbi sui dati non usati in ottimizzazione nel backtest",
     "Verificare che non escluda trade 'a giochi fatti' per gonfiare la performance, e che quei trade "
     "sarebbero davvero stati aperti o chiusi dal bot in tempo reale.", "alta"),
    ("seed-0011", "Il trading automatico non ha mai piazzato un ordine",
     "Trovato lavorando sugli alert, non segnalato: non c'era modo di accorgersene. "
     "Nessun errore, nessun log, semplicemente nessun ordine.", "alta"),
    ("seed-0013", "Il sito non carica piu': resta la schermata vuota",
     "Segnalato con screenshot da telefono: nav e testo iniziale visibili, ma niente funziona.", "alta"),
    ("seed-0014", "La pagina resta vecchia dopo un deploy",
     "Il log dell'ottimizzazione mostrava ancora la versione precedente mentre in produzione c'era gia' quella nuova.", "alta"),
    ("seed-0015", "L'ottimizzazione fallisce con un errore che non parla di ottimizzazione",
     "'The string did not match the expected pattern', e nessun modo di sapere se sta lavorando.", "alta"),
    ("seed-0016", "La ricerca sembra tirare i dadi",
     "Deve capire la strategia e concentrarsi come fa un navigatore, non provare ogni strada.", "alta"),
    ("seed-0017", "La strategia non tiene conto della volatilita' delle coppie poco liquide",
     "Su altcoin che si muovono molto senza un trend vero rende male.", "alta"),
    ("seed-0018", "Se chiudo il browser perdo l'ottimizzazione in corso",
     "Deve continuare, avvisarmi su Telegram alla fine e restare salvata in un archivio.", "alta"),
    ("seed-0019", "Il sito diventa irraggiungibile mentre gira una ricerca",
     "Segnalato dal vivo durante una ricerca stabile.", "alta"),
    ("seed-0020", "L'indicatore non e' allineato al prezzo",
     "Segnalato con screenshot: il pannello sotto il grafico non e' agganciato alle stesse candele.", "alta"),
    ("seed-0022", "Lo stesso account vede il tema chiaro su un computer e scuro su un altro",
     "Gabriele dal suo pc non vede lo sfondo bianco.", "media"),
    ("seed-0023", "Le segnalazioni sono tante e non si trovano",
     "Serve un elenco richiudibile e una ricerca per parole chiave.", "media"),
    ("seed-0024", "Esportare i trade del backtest in Excel",
     "Cliccando sul numero di trade, poter scaricare tutto in un file con i grafici per "
     "riverificare il portafoglio.", "media"),
    ("seed-0025", "Errore 422 quando si avvia la strategia",
     "Premendo per far girare la strategia arriva 'Errore 422' e non parte niente.", "alta"),
    ("seed-0021", "Chiudendo il log non si sa piu' a che punto e' l'ottimizzazione",
     "Serve un indicatore che resti visibile finche' non e' salvata in archivio, e poter lanciare "
     "un'altra ricerca senza aspettare la prima.", "media"),
    ("seed-0012", "Voci della lista iniziale che non comparivano piu' sulla bacheca",
     "Trovato controllando la bacheca dal vivo: seed-0011 non c'era, e la sua risoluzione "
     "restava senza voce a cui agganciarsi.", "media"),
    ("seed-0026", "Visualizzare in modo stilizzato cosa sta facendo la ricerca stabile",
     "Durante la ricerca stabile, un'animazione che mostri graficamente l'equivalente di "
     "quello che l'algoritmo sta facendo — non serve fedele al dettaglio, basta che sia "
     "bella e minimale.", "bassa"),
    ("seed-0027", "Operazioni futures: leva, margine, liquidazione",
     "Introdurre le operazioni futures — completo, accurato e a prova di errore: leva, "
     "margine isolated, liquidazione intrabar, funding. Prima solo in backtest, esecuzione "
     "reale solo dopo validazione, exchange per exchange.", "media"),
]


def _seed() -> None:
    """Aggiunge le voci della lista iniziale che mancano, una per una.

    Gli id sono fissi e non generati: le risoluzioni scritte da Claude si
    agganciano per id, e un id casuale al primo avvio scollegherebbe le
    diagnosi gia' scritte da queste voci.

    Il controllo e' per id, non "l'elenco e' vuoto". Con quest'ultimo, una voce
    aggiunta alla lista dopo il primo avvio non compariva mai piu': il file sul
    volume esisteva gia', quindi il seeding usciva subito. E' successo con
    seed-0011, che dal vivo non c'era mentre la sua risoluzione, scritta e
    distribuita, restava senza voce a cui agganciarsi.

    Una voce cancellata a mano resta cancellata: `seeded_ids.json` ricorda cosa
    e' gia' stato inserito, cosi' "mai vista" e "vista e buttata" non si
    confondono.
    """
    now = int(time.time())
    have = {it.get("id") for it in _issues}
    added = False
    for i, (iid, title, detail, prio) in enumerate(_SEED):
        if iid in have or iid in _seeded_ids:
            _seeded_ids.add(iid)
            continue
        _issues.append({
            "id": iid, "title": title, "detail": detail, "priority": prio,
            "status": "aperto", "author": "lista iniziale",
            "created_at": now - (len(_SEED) - i),
        })
        _seeded_ids.add(iid)
        added = True
    if added:
        _save()
    _save_seeded()


_load()
_load_seeded()
_load_replies()
_seed()


def list_issues() -> list[dict]:
    """Segnalazioni con la risoluzione agganciata, le più recenti per prime."""
    res = _resolutions()
    mine = _claude_replies()
    out = []
    for it in _issues:
        merged = dict(it)
        r = res.get(it["id"])
        if r:
            merged["resolution"] = r
            # La risoluzione è la fonte di verità sullo stato: se Claude l'ha
            # segnata risolta, l'elenco non deve continuare a mostrarla aperta
            # solo perché nessuno ha aggiornato il campo a mano.
            if r.get("status") in _VALID_STATUS:
                merged["status"] = r["status"]
        # …tranne quando chi l'ha scritta dice che il problema si vede ancora.
        # Una risoluzione senza data si considera piu' vecchia di qualsiasi
        # riapertura: chiudere per decreto una voce riaperta sarebbe il modo
        # piu' veloce di rendere inutile la bacheca.
        reopened = int(it.get("reopened_at", 0) or 0)
        if reopened and reopened > int((r or {}).get("at", 0) or 0):
            merged["status"] = "riaperto"
        thread = [dict(x, side="loro") for x in _replies if x.get("issue_id") == it["id"]]
        thread += [dict(x, side="claude") for x in mine.get(it["id"], [])]
        merged["replies"] = sorted(thread, key=lambda x: x.get("created_at", 0))
        out.append(merged)
    return sorted(out, key=lambda x: x.get("created_at", 0), reverse=True)


def add_issue(title: str, detail: str = "", priority: str = "media",
              author: str = "") -> dict:
    title = (title or "").strip()
    if not title:
        raise ValueError("Titolo mancante")
    entry = {
        "id": uuid.uuid4().hex[:8],
        "title": title[:300],
        "detail": (detail or "").strip()[:4000],
        "priority": priority if priority in _VALID_PRIORITY else "media",
        "status": "aperto",
        "author": (author or "").strip()[:120],
        "created_at": int(time.time()),
    }
    _issues.append(entry)
    if len(_issues) > _MAX_ISSUES:
        del _issues[: len(_issues) - _MAX_ISSUES]
    _save()
    return entry


def update_issue(issue_id: str, *, status: str | None = None,
                 priority: str | None = None, detail: str | None = None) -> dict | None:
    for it in _issues:
        if it["id"] != issue_id:
            continue
        if status is not None:
            if status not in _VALID_STATUS:
                raise ValueError(f"Stato non valido: {status}")
            it["status"] = status
            # list_issues() rilegge lo stato dalla risoluzione quando ce n'e'
            # una: senza segnare anche qui la riapertura, un "Riapri" manuale
            # spariva al giro dopo perche' la vecchia risoluzione "risolto"
            # tornava a vincere sul merge.
            if status in ("aperto", "riaperto"):
                it["reopened_at"] = int(time.time())
        if priority is not None:
            if priority not in _VALID_PRIORITY:
                raise ValueError(f"Priorità non valida: {priority}")
            it["priority"] = priority
        if detail is not None:
            it["detail"] = detail.strip()[:4000]
        _save()
        return it
    return None


def delete_issue(issue_id: str) -> bool:
    for i, it in enumerate(_issues):
        if it["id"] == issue_id:
            del _issues[i]
            _save()
            return True
    return False
