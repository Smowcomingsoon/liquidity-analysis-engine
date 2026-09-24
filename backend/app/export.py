"""
Il backtest come foglio di calcolo, con i grafici dentro.

Serve a rifare i conti fuori dall'app: aprire il file, guardare la curva del
capitale, ordinare i trade per perdita, sommare una colonna a mano. Un'immagine
del risultato non lo permette, un elenco a schermo nemmeno.

Due scelte che vale la pena dichiarare:

  * **I totali sono formule, non numeri incollati.** Se cancelli tre righe di
    trade il riepilogo si aggiorna. Un file in cui il totale resta quello di
    prima mentre le righe cambiano e' peggio di un file senza totale, perche'
    sembra ancora giusto.
  * **Le percentuali sono frazioni.** Il backtest le porta come 4.7 per "4,7%";
    scritte cosi' con formato percentuale Excel mostrerebbe 470%. Vengono
    divise per cento all'ingresso, una volta sola.

I numeri li rimisura il server rieseguendo il backtest, non li accetta dal
browser: un foglio costruito su quello che il client afferma sarebbe un foglio
di affermazioni.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone

from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.models import BacktestResult

_FONT = "Arial"
_HEAD_FILL = PatternFill("solid", fgColor="1F2A37")
_HEAD_FONT = Font(name=_FONT, bold=True, color="FFFFFF", size=10)
_LABEL = Font(name=_FONT, bold=True, size=10)
_BODY = Font(name=_FONT, size=10)
_INPUT = Font(name=_FONT, size=10, color="0000FF")   # convenzione: blu = dato immesso

_MONEY = '#,##0.00;[Red](#,##0.00);-'
_PCT = '0.00%;[Red]-0.00%;-'
_DATE = "dd/mm/yyyy hh:mm"


def _ts(value: int | None) -> datetime | None:
    """Istante Excel da un timestamp unix, in secondi o in millisecondi.

    Il backtest usa entrambe le unita': i trade e la curva in secondi, l'inizio
    e la fine dei dati in millisecondi. Trattarli tutti come secondi dava l'anno
    53971. Il discrimine e' netto — 1e11 secondi sono l'anno 5138, quindi
    qualunque valore piu' grande e' in millisecondi e non c'e' ambiguita'."""
    if not value:
        return None
    n = int(value)
    if n > 100_000_000_000:
        n //= 1000
    return datetime.fromtimestamp(n, tz=timezone.utc).replace(tzinfo=None)


def _sheet_header(ws, row: int, labels: list[str]) -> None:
    for col, text in enumerate(labels, start=1):
        c = ws.cell(row=row, column=col, value=text)
        c.font = _HEAD_FONT
        c.fill = _HEAD_FILL
        c.alignment = Alignment(horizontal="center", vertical="center")
    ws.freeze_panes = ws.cell(row=row + 1, column=1)


def _widths(ws, widths: dict[int, int]) -> None:
    for col, w in widths.items():
        ws.column_dimensions[get_column_letter(col)].width = w


def _trades_sheet(wb: Workbook, title: str, trades: list, initial_capital: float):
    """Un foglio di operazioni, con il P&L cumulato calcolato dal foglio stesso.

    Il capitale dopo ogni trade lo porta gia' il backtest, ma la colonna
    cumulata e' una formula: e' quella che permette di cancellare una riga e
    vedere cosa sarebbe successo senza quel trade, che e' meta' del motivo per
    cui si esporta.
    """
    ws = wb.create_sheet(title)
    _sheet_header(ws, 1, ["#", "Direction", "Entry", "Exit", "Days",
                          "Entry price", "Exit price", "P&L %", "P&L",
                          "Equity after", "Cumulative P&L"])
    for i, t in enumerate(trades, start=1):
        r = i + 1
        entry, exit_ = _ts(t.entry_time), _ts(t.exit_time)
        ws.cell(row=r, column=1, value=i).font = _BODY
        ws.cell(row=r, column=2, value="Long" if t.direction == "long" else "Short").font = _BODY
        for col, when in ((3, entry), (4, exit_)):
            c = ws.cell(row=r, column=col, value=when)
            c.font = _BODY
            c.number_format = _DATE
        c = ws.cell(row=r, column=5, value=f"=IF(AND(C{r}<>\"\",D{r}<>\"\"),D{r}-C{r},\"\")")
        c.font = _BODY
        c.number_format = "0.0"
        for col, val in ((6, t.entry_price), (7, t.exit_price)):
            c = ws.cell(row=r, column=col, value=float(val))
            c.font = _BODY
            c.number_format = "#,##0.00000000"
        c = ws.cell(row=r, column=8, value=float(t.pnl_pct) / 100.0)
        c.font = _BODY
        c.number_format = _PCT
        c = ws.cell(row=r, column=9, value=float(t.pnl_abs))
        c.font = _BODY
        c.number_format = _MONEY
        c = ws.cell(row=r, column=10, value=float(t.equity_after))
        c.font = _BODY
        c.number_format = _MONEY
        c = ws.cell(row=r, column=11, value=f"=SUM($I$2:I{r})")
        c.font = _BODY
        c.number_format = _MONEY

    last = len(trades) + 1
    if trades:
        r = last + 1
        ws.cell(row=r, column=1, value="Total").font = _LABEL
        c = ws.cell(row=r, column=9, value=f"=SUM(I2:I{last})")
        c.font = _LABEL
        c.number_format = _MONEY
        c = ws.cell(row=r, column=8, value=f"=I{r}/{initial_capital}")
        c.font = _LABEL
        c.number_format = _PCT

    _widths(ws, {1: 5, 2: 10, 3: 17, 4: 17, 5: 7, 6: 16, 7: 16, 8: 10, 9: 12, 10: 14, 11: 14})
    return ws, last


def _pnl_chart(ws, last_row: int, anchor: str) -> None:
    """Il risultato di ogni operazione, in barre. Serve a vedere se il totale
    viene da molti trade simili o da uno solo enorme — due storie diverse che
    la percentuale finale racconta uguali."""
    if last_row < 2:
        return
    ch = BarChart()
    ch.type = "col"
    ch.title = "Result per trade"
    ch.y_axis.title = "P&L"
    ch.x_axis.title = "Trade"
    ch.height, ch.width = 7.5, 24
    ch.add_data(Reference(ws, min_col=9, min_row=1, max_row=last_row), titles_from_data=True)
    ch.set_categories(Reference(ws, min_col=1, min_row=2, max_row=last_row))
    ch.legend = None
    ws.add_chart(ch, anchor)


def build_backtest_workbook(
    bt: BacktestResult,
    *,
    symbol: str,
    strategy_name: str,
    chart_interval: str,
    params: dict,
    backtest_params: dict,
) -> bytes:
    wb = Workbook()

    # --- Riepilogo ---------------------------------------------------------
    ws = wb.active
    ws.title = "Summary"
    ws["A1"] = "Backtest"
    ws["A1"].font = Font(name=_FONT, bold=True, size=14)
    rows = [
        ("Pair", symbol.upper()),
        ("Strategy", strategy_name),
        ("Backtest timeframe", bt.interval),
        ("Chart timeframe", chart_interval),
        ("From", _ts(bt.data_start)),
        ("To", _ts(bt.data_end)),
        ("Exported on", datetime.now(tz=timezone.utc).replace(tzinfo=None)),
    ]
    r = 3
    for label, value in rows:
        ws.cell(row=r, column=1, value=label).font = _LABEL
        c = ws.cell(row=r, column=2, value=value)
        c.font = _BODY
        if isinstance(value, datetime):
            c.number_format = _DATE
        r += 1

    r += 1
    ws.cell(row=r, column=1, value="Result").font = Font(name=_FONT, bold=True, size=12)
    r += 1
    n = bt.num_trades
    # I totali sono formule sul foglio delle operazioni: cancellando righe il
    # riepilogo segue, invece di restare a raccontare un file che non esiste piu'.
    metrics = [
        ("Initial capital", bt.initial_capital, _MONEY, True),
        ("Final capital", f"=B{r}+Trade!I{n + 2}" if n else bt.final_equity, _MONEY, False),
        ("Return", f"=IF(B{r}<>0,B{r + 1}/B{r}-1,\"\")", _PCT, False),
        ("Trades", f"=COUNT(Trade!A2:A{n + 1})" if n else 0, "0", False),
        ("Winners", f"=COUNTIF(Trade!I2:I{n + 1},\">0\")" if n else 0, "0", False),
        ("Win rate", f"=IF(B{r + 3}>0,B{r + 4}/B{r + 3},\"\")" if n else "", _PCT, False),
        ("Max drawdown", float(bt.max_drawdown_pct) / 100.0, _PCT, False),
        ("Profit factor", bt.profit_factor, "0.00", False),
        ("Sharpe", bt.sharpe_ratio, "0.00", False),
    ]
    for label, value, fmt, is_input in metrics:
        ws.cell(row=r, column=1, value=label).font = _LABEL
        c = ws.cell(row=r, column=2, value=value)
        c.font = _INPUT if is_input else _BODY
        c.number_format = fmt
        r += 1

    r += 1
    ws.cell(row=r, column=1, value="Strategy parameters").font = Font(name=_FONT, bold=True, size=12)
    r += 1
    for k, v in (params or {}).items():
        ws.cell(row=r, column=1, value=str(k)).font = _BODY
        ws.cell(row=r, column=2, value=v if isinstance(v, (int, float, str)) else str(v)).font = _INPUT
        r += 1
    r += 1
    ws.cell(row=r, column=1, value="Backtest parameters").font = Font(name=_FONT, bold=True, size=12)
    r += 1
    for k, v in (backtest_params or {}).items():
        ws.cell(row=r, column=1, value=str(k)).font = _BODY
        ws.cell(row=r, column=2, value=v if isinstance(v, (int, float, str)) else str(v)).font = _INPUT
        r += 1

    ws.cell(row=r + 1, column=1,
            value="Inputs in blue; everything else is formulas on the Trade and Equity sheets.").font = _BODY
    _widths(ws, {1: 30, 2: 26})

    # --- Trade -------------------------------------------------------------
    ws_tr, last = _trades_sheet(wb, "Trade", bt.trades, bt.initial_capital)
    _pnl_chart(ws_tr, last, "M2")

    if bt.oos_trades:
        ws_oos, last_oos = _trades_sheet(wb, "Out-of-sample trades", bt.oos_trades,
                                         bt.initial_capital)
        _pnl_chart(ws_oos, last_oos, "M2")

    # --- Equity ------------------------------------------------------------
    ws_eq = wb.create_sheet("Equity")
    _sheet_header(ws_eq, 1, ["Date", "Equity", "Peak", "Drawdown"])
    curve = bt.equity_curve or []
    for i, (t, v) in enumerate(curve, start=2):
        c = ws_eq.cell(row=i, column=1, value=_ts(t))
        c.font = _BODY
        c.number_format = _DATE
        c = ws_eq.cell(row=i, column=2, value=float(v))
        c.font = _BODY
        c.number_format = _MONEY
        # Il picco e il calo dal picco sono formule: e' il conto che chiunque
        # rifarebbe a mano per controllare il drawdown dichiarato.
        c = ws_eq.cell(row=i, column=3, value=f"=MAX($B$2:B{i})")
        c.font = _BODY
        c.number_format = _MONEY
        c = ws_eq.cell(row=i, column=4, value=f"=IF(C{i}>0,B{i}/C{i}-1,\"\")")
        c.font = _BODY
        c.number_format = _PCT
    _widths(ws_eq, {1: 18, 2: 14, 3: 16, 4: 12})

    if len(curve) > 1:
        last_eq = len(curve) + 1
        ch = LineChart()
        ch.title = f"Equity — {symbol.upper()} · {strategy_name}"
        ch.y_axis.title = "Equity"
        ch.x_axis.title = "Date"
        ch.height, ch.width = 9, 26
        ch.add_data(Reference(ws_eq, min_col=2, min_row=1, max_row=last_eq), titles_from_data=True)
        ch.add_data(Reference(ws_eq, min_col=3, min_row=1, max_row=last_eq), titles_from_data=True)
        ch.set_categories(Reference(ws_eq, min_col=1, min_row=2, max_row=last_eq))
        ws_eq.add_chart(ch, "F2")

        dd = LineChart()
        dd.title = "Drawdown from peak"
        dd.height, dd.width = 6, 26
        dd.add_data(Reference(ws_eq, min_col=4, min_row=1, max_row=last_eq), titles_from_data=True)
        dd.set_categories(Reference(ws_eq, min_col=1, min_row=2, max_row=last_eq))
        dd.legend = None
        ws_eq.add_chart(dd, "F22")

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
