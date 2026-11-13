"""Temporary probe: reproduce the BotDetailPane crash at 206x65 with closes loaded and Benter selected."""
import test_bots as TB  # noqa: F401
from test_bots import make, until  # noqa: F401

from glimpse_tui import botsview


async def test_probe(make):
    app = make()
    async with app.run_test(size=(206, 65)) as pilot:
        await pilot.pause()
        await pilot.press("B")
        await until(pilot, lambda: len(app.bots) > 0)
        await pilot.pause(0.3)
        # walk to Benter and render the detail pane directly so the exception surfaces here
        names = [b.id for b in botsview.visible(app)]
        app.bot_cur = names.index("benter")
        app.paint()
        await pilot.pause(0.3)
        pane = app.query_one("#botdetail")
        for b in botsview.visible(app):
            app.bot_cur = names.index(b.id)
            try:
                pane.render()
            except Exception as e:
                print(f"\nRENDER FAIL on {b.id}: {type(e).__name__}: {e}")
                import traceback
                traceback.print_exc()
                raise
        print("\nall bots rendered clean at 206x65")
