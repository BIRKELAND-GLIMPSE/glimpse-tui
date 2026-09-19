"""Function pages, one module per family. `term/registry.py` imports each family once and a page registers itself.

How to write a page (funcs/btc.py is the worked example):

1. Subclass `term.panes.FuncPane`. Set `code`, `every` (seconds between loads, 0 for once) and, for a list with a
   row cursor, `selectable = True`.
2. `async def load(self)`: fetch through `self.hub.sources[...]`, keep the results on `self`, set `self.title` and
   `self.provs` (every `Provenance` behind what is on screen). Raise `SourceError` only when there is nothing at all
   to show. Walk `self.hub.bitcoin_order()` for Bitcoin data so a self-hosted backend or a fallback can answer.
   `load` runs in a worker. It must never block: no sync I/O, no sleeps, parsing heavier than a few milliseconds
   goes through `asyncio.to_thread`.
3. `def draw(self, w, h) -> list[Text]`: build lines with `term.ui` (kv, section, table, bar_row, signed_pct, usd,
   sats_usd) and `charts` (Plot, spark, vbars, pixels, stacked). Read only what `load` stored and the hub's cached
   state (`hub.chain`, `hub.quotes`). Never await, never fetch. More lines than `h` scroll. A page must read well
   at 40 columns and at 200. Keep a redraw under 10 ms: resample series to the plot width before drawing.
4. Optional: `key(k, ch)` for page keys, `enter()` returning a GO bar command for the cursor row, `menu()` for the
   digit shortcuts, `export()` returning (header, rows) for EXP, `hint()` for the bottom edge, `cache_key()` for
   any state outside the pane that changes the picture.
5. `register(Function(code, name, category, summary, PaneClass, takes=..., args=..., help=...))`. The help text is
   the HELP page: what it shows, where each number comes from, how often it refreshes, what happens when a source
   is down. Plain sentences, one idea each, no em-dashes.

Rules that matter everywhere (TERMINAL.md section 2): say where every number came from and how old it is; label
every proxy and every daily value; sats as ₿ with dollars beside them, fees in sat/vB, hashrate in EH/s, UTC;
never invent an identifier; every glyph one cell wide; tests run on tests/fixtures/sources/ and never the network.
"""
