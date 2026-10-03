"""Playlist mouse focus and pane navigation regressions."""

import asyncio

import pytest

from tests.test_tui import StubClient, settle
from ytm.tui.app import YTMApp


@pytest.mark.parametrize('size', [(80, 24), (120, 40), (180, 24)])
def test_playlist_shortcut_from_search(size):
    async def scenario():
        app = YTMApp(client=StubClient())
        async with app.run_test(size=size) as pilot:
            await settle(pilot)
            assert app.focused.id == 'search-input'
            app.action_focus_playlists()
            await settle(pilot)
            assert app.focused.id == 'playlists-table'
            await pilot.press('down')
            await settle(pilot)
            assert app.focused.id == 'playlists-table'
    asyncio.run(scenario())


def test_tab_from_search_reaches_playlists():
    async def scenario():
        app = YTMApp(client=StubClient())
        async with app.run_test(size=(164, 46)) as pilot:
            await settle(pilot)
            seen = []
            for _ in range(5):
                await pilot.press('tab')
                await settle(pilot)
                seen.append(app.focused.id)
            assert 'playlists-table' in seen, seen
    asyncio.run(scenario())


def test_panes_shortcut_moves_focus_out_of_search():
    async def scenario():
        app = YTMApp(client=StubClient())
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            app.action_cycle_pane()
            await settle(pilot)
            assert app.focused.id != 'search-input'
    asyncio.run(scenario())


@pytest.mark.parametrize('target', ['#playlists-title', '#playlists-pane'])
def test_clicking_playlist_pane_takes_focus_from_search(target):
    async def scenario():
        app = YTMApp(client=StubClient())
        async with app.run_test(size=(164, 46)) as pilot:
            await settle(pilot)
            await pilot.click(target)
            await settle(pilot)
            assert app.focused.id == 'playlists-table'
    asyncio.run(scenario())
