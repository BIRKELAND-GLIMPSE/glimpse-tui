"""Today's screens as functions (TERMINAL.md 8): ODDS, HM, SLIP, PORT and BOTS.

They open the view they always had, full stage, with every key they always had (PLAN D31). Nothing about
ordering changes: the Confirm dialog and the server-estimate gate are the app's, untouched.
"""
from __future__ import annotations

from ..term.registry import Function, register

_DOWN = "It reads the Glimpse API only. When the API is unreachable the last markets stay on screen and the status line says so."

register(Function(
    "ODDS", "Odds", "Glimpse", "the odds on every outcome, close by close, and the bet that takes them", None, legacy="main",
    help="Every live close of the chosen series on the left, with the market's median and 80% band. On the right, the odds on "
         "every outcome of the close you are on: each price range with its probability, what it pays net of fees, and your "
         "position in it. Prices come from the public Glimpse API: the market list every 60 s, the focused book every 5 s. "
         "o opens it from anywhere. Keys are the ones the screen has always had: j k move, h l switch pane, v selects a range, "
         "b buys after a confirmation and a server estimate. [ and ] change series. It was called MKT, and the ladder, before "
         "it was called the odds; MKT still opens it. " + _DOWN))
register(Function(
    "HM", "Forecast", "Glimpse", "the forecast: every close, every price, one picture", None, legacy="heatmap",
    help="The forecast heatmap, which f opens from anywhere. Time runs across, price runs up, and each character is one "
         "price cell of one close, denser where the market "
         "puts more probability. History candles sit left of NOW on the same axis. The cyan rule is each close's median. "
         "v draws a box, tab opens the bet slip, b bets after a confirmation. Data: the public Glimpse API every 60 s, "
         "Coinbase candles every 120 s. " + _DOWN))
register(Function(
    "SLIP", "Bet slip", "Glimpse", "the bet slip beside the heatmap", None, legacy="heatmap",
    help="Opens the heatmap with the bet slip focused: Low, High, From, To and Size, then cost, payout, profit, odds "
         "and ROI. Editing the slip moves the box on the chart. The slip needs 118 columns; narrower terminals show the "
         "compact ticket. Orders go through the same confirmation and estimate gate as everywhere else."))
register(Function(
    "PORT", "Portfolio", "Glimpse", "your open positions, value and P&L", None, legacy="portfolio",
    help="Open positions with cost, value and profit, priced locally from the live books. x sells the selected "
         "position after a confirmation. Needs an API key (L). Reads the keyed Glimpse API every 15 s, inside the "
         "60 requests a minute the API allows. Trade history is not available to API keys (PLAN F4)."))
register(Function(
    "BOTS", "Bots", "Glimpse", "the model zoo: 130 forecasting bots that run on this computer", None, legacy="bots",
    args="[model id]",
    help="Every Forecast Lab model that runs on hourly candles alone, read against the live market. `BOTS <model id>` opens on "
         "one model, which is what enter does on the CONS and BOT windows of the front page. Tabs sort them by "
         "their live view. i explains a model, enter runs one on paper, and with a key and the word LIVE, with real sats. "
         "Data: public Coinbase hourly candles and the public Glimpse API."))
