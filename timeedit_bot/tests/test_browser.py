"""Browser-driven tests (Playwright + Chromium) against the mock's fake Feide SSO.

Skipped automatically when Playwright or its Chromium is not installed.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from te_bot.auth import automated_feide_login
from te_bot.config import Config
from te_bot.learn import learn
from te_bot.mock_server import MockTimeEdit, start_server
from te_bot.timeedit import TimeEditClient

playwright = pytest.importorskip("playwright")


@pytest.fixture()
def browser_env(tmp_path: Path):
    from playwright.sync_api import sync_playwright

    try:  # skip cleanly when no browser binary is available
        with sync_playwright() as p:
            p.chromium.launch(headless=True).close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Chromium not available: {exc}")
    mock = MockTimeEdit()
    server, base_url = start_server(mock)
    cfg = Config(home=tmp_path)
    cfg.timeedit.base_url = base_url
    cfg.auth.headless = True
    cfg.ensure_dirs()
    try:
        yield mock, cfg
    finally:
        server.shutdown()


def test_automated_login_through_fake_feide(browser_env) -> None:
    mock, cfg = browser_env
    state = automated_feide_login(cfg, "student", "secret", timeout_s=60, headless=True)
    names = {c["name"] for c in state["cookies"]}
    assert mock.cookie_name in names
    assert cfg.storage_state_file.exists()
    client = TimeEditClient(cfg.timeedit, state)
    assert client.is_logged_in()


def test_automated_login_reports_wrong_password(browser_env) -> None:
    mock, cfg = browser_env
    with pytest.raises(RuntimeError):
        automated_feide_login(cfg, "student", "wrong", timeout_s=12, headless=True)


def test_learn_captures_a_scripted_booking(browser_env) -> None:
    mock, cfg = browser_env
    automated_feide_login(cfg, "student", "secret", timeout_s=60, headless=True)
    day = (mock.today or date.today()) + timedelta(days=2)
    room = mock.rooms[3][0]  # some room object id, e.g. "1003.186"

    def make_booking(page) -> None:
        # Script what a human would do in the UI: the page posts CSTTG=gen, then kind=reserve.
        page.evaluate(
            """async ([url, dates, room]) => {
                const headers = {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8', 'X-Requested-With': 'XMLHttpRequest'};
                const tok = await (await fetch(url, {method: 'POST', headers, body: 'CSTTG=gen'})).text();
                const body = new URLSearchParams([['kind','reserve'],['nocache','4'],['l','en_GB'],['o', room],['o','777.190'],
                    ['aos',''],['dates', dates],['starttime','10:00'],['endtime','11:00'],['url', url],['fe2','Group work'],['CSTT', tok.trim()]]);
                await fetch(url, {method: 'POST', headers, body: body.toString(), redirect: 'manual'});
            }""",
            [cfg.timeedit.reserve_url, day.strftime("%Y%m%d"), room],
        )

    template = learn(cfg, timeout_s=60, ask=None, on_page=make_booking, headless=True)
    assert template["room_object"] == room  # guessed from the ids present in the page
    assert template["room_type"] == str(mock.room_type)
    fields = dict(template["fields"])
    assert fields["kind"] == "reserve" and fields["fe2"] == "Group work"
    assert [v for k, v in template["fields"] if k == "o"] == [room, "777.190"]
    assert template["response"]["status"] == 302 and "id=" in template["response"]["location"]
    assert Path(template["har"]).exists()
    saved = json.loads(cfg.template_file.read_text())
    assert saved["room_object"] == room
    # And the client replays it with a different room/day/time, keeping the extra object and fe2.
    client = TimeEditClient(cfg.timeedit, json.loads(cfg.storage_state_file.read_text()), saved)
    result = client.reserve(mock.rooms[5][0], day + timedelta(days=1), "12:00", "16:00", client.get_csrf())
    assert result.outcome.value == "success", result.message
    sent = mock.attempts[-1]
    assert sent["objects"] == [mock.rooms[5][0], "777.190"] and sent["fields"]["fe2"] == "Group work"
