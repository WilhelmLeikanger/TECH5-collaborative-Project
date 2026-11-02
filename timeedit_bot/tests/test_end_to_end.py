"""End-to-end tests against the built-in mock TimeEdit server."""

from __future__ import annotations

import time
from datetime import date, timedelta
from pathlib import Path

import pytest

from te_bot.config import Config
from te_bot.discover import discover
from te_bot.mock_server import MockTimeEdit, start_server
from te_bot.sniper import estimate_offset, snipe
from te_bot.state import State
from te_bot.strategy import build_candidates, resolve_rooms
from te_bot.timeedit import NotLoggedIn, Outcome, TEObject, TimeEditClient


@pytest.fixture()
def mock_env(tmp_path: Path):
    mock = MockTimeEdit(blocked_rooms=("Grupperom U28",))
    server, base_url = start_server(mock)
    cfg = Config(home=tmp_path)
    cfg.timeedit.base_url = base_url
    cfg.strategy.rooms = ["Grupperom U28", "Grupperom U30", "Grupperom U29"]
    cfg.strategy.any_room_fallback = False
    cfg.strategy.windows = ["08:00-12:00", "12:00-16:00"]
    cfg.release.lead_ms = 30
    cfg.release.fast_retry_ms = 60
    cfg.release.min_request_gap_ms = 20
    cfg.ensure_dirs()
    cookies = {mock.cookie_name: mock.cookie_value}
    try:
        yield mock, cfg, cookies
    finally:
        server.shutdown()


def _client(cfg: Config, cookies: dict[str, str], template: dict | None = None) -> TimeEditClient:
    return TimeEditClient(cfg.timeedit, cookies, template)


def test_session_and_discovery(mock_env) -> None:
    mock, cfg, cookies = mock_env
    client = _client(cfg, cookies)
    assert client.is_logged_in()
    assert not _client(cfg, {"TEnhhweb": "wrong"}).is_logged_in()
    with pytest.raises(NotLoggedIn):
        _client(cfg, {"TEnhhweb": "wrong"}).my_reservations()
    result = discover(cfg, client)
    assert result["sid"] == 12
    assert result["room_type"] == mock.room_type
    names = {r["name"] for r in result["rooms"]}
    assert "Grupperom U28" in names and len(names) == len(mock.rooms)
    assert any(f["name"] == "fe2" for f in result["form_fields"])
    assert result["grid"]["days"]
    assert cfg.discovered_file.exists()


def test_snipe_waits_for_release_then_falls_back_to_free_room(mock_env) -> None:
    mock, cfg, cookies = mock_env
    mock.release_at = time.time() + 1.2
    cfg.release.lead_ms = 400  # fire deliberately early so the "not open yet" retry path is exercised
    client = _client(cfg, cookies)
    day = mock.today + timedelta(days=mock.days_ahead)
    rooms = resolve_rooms(cfg.strategy, cfg.timeedit, [TEObject(o.split(".")[0], o.split(".")[1], n) for o, n in mock.rooms])
    candidates = build_candidates(cfg.strategy, rooms)
    assert candidates[0].room.name == "Grupperom U28"
    sync = estimate_offset(client, max_samples=15)
    fire_at = mock.release_at - sync.offset_s - cfg.release.lead_ms / 1000.0
    report = snipe(client, candidates, day, cfg, fire_at)
    assert report.success, report.summary()
    booked, result = report.booked[0]
    assert booked.room.name == "Grupperom U30"  # U28 is blocked
    assert booked.hours == 4 and booked.start == "08:00"
    assert result.reservation_id and result.status == 302
    outcomes = [a.result.outcome for a in report.attempts]
    assert Outcome.NOT_OPEN in outcomes  # we started firing before the release
    assert Outcome.OCCUPIED in outcomes  # U28 rejected, moved on
    first_success = next(a for a in report.attempts if a.result.outcome is Outcome.SUCCESS)
    assert first_success.at - mock.release_at < 0.5  # booked within half a second of the release
    assert client.my_reservations()[0]["room"] == "Grupperom U30"


def test_snipe_shortens_when_too_long_and_stops_on_quota(mock_env) -> None:
    mock, cfg, cookies = mock_env
    mock.max_hours = 2
    client = _client(cfg, cookies)
    day = mock.today + timedelta(days=3)
    rooms = resolve_rooms(cfg.strategy, cfg.timeedit, [TEObject(o.split(".")[0], o.split(".")[1], n) for o, n in mock.rooms])
    candidates = build_candidates(cfg.strategy, rooms)
    report = snipe(client, candidates, day, cfg, time.time())
    assert report.success
    assert report.booked[0][0].hours == 2
    outcomes = [a.result.outcome for a in report.attempts]
    assert Outcome.LENGTH in outcomes
    # Now fill the weekly quota and try again: the bot must stop, not loop.
    for i in range(1, 4):
        mock.add_reservation(mock.rooms[0][0], day + timedelta(days=i) if (day + timedelta(days=i)).isocalendar()[:2] == day.isocalendar()[:2] else day, 18 * 60, 19 * 60, user="me")
    report = snipe(client, build_candidates(cfg.strategy, rooms), day, cfg, time.time())
    assert not report.success
    assert report.stop_reason.startswith("quota")


def test_template_is_used_and_cancel_works(mock_env) -> None:
    mock, cfg, cookies = mock_env
    template = {
        "url": f"{cfg.timeedit.base_url}/{cfg.timeedit.reserve_page}",
        "room_object": "1000.186",
        "fields": [
            ["kind", "reserve"],
            ["nocache", "4"],
            ["o", "1000.186"],
            ["o", "777.190"],
            ["aos", ""],
            ["dates", "20260101"],
            ["starttime", "16:00"],
            ["endtime", "17:00"],
            ["fe2", "Group work"],
            ["CSTT", "0"],
        ],
    }
    client = _client(cfg, cookies, template)
    day = mock.today + timedelta(days=2)
    csrf = client.get_csrf()
    result = client.reserve("1002.186", day, "10:00", "12:00", csrf)
    assert result.outcome is Outcome.SUCCESS
    sent = mock.attempts[-1]
    assert sent["objects"] == ["1002.186", "777.190"]
    assert sent["fields"]["fe2"] == "Group work"
    ok, text = client.cancel(result.reservation_id or "")
    assert ok, text
    assert client.my_reservations() == []


def test_availability_filter_uses_grid(mock_env) -> None:
    mock, cfg, cookies = mock_env
    client = _client(cfg, cookies)
    day = mock.today + timedelta(days=2)
    u30 = next(o for o, n in mock.rooms if n == "Grupperom U30")
    mock.add_reservation(u30, day, 9 * 60, 10 * 60)  # Grupperom U30 busy 09-10
    from te_bot.schedule_parser import parse_grid

    html = client.grid_html([o for o, _ in mock.rooms], day)
    grid = parse_grid(html)
    busy = grid.busy(day.isoformat())
    assert busy[u30] == [(9 * 60, 10 * 60)]
    rooms = resolve_rooms(cfg.strategy, cfg.timeedit, [TEObject(o.split(".")[0], o.split(".")[1], n) for o, n in mock.rooms])
    candidates = build_candidates(cfg.strategy, rooms, busy)
    u30_cands = [c for c in candidates if c.room.name == "Grupperom U30"]
    assert u30_cands and not any(c.overlaps(9 * 60, 10 * 60) for c in u30_cands)
    assert any(c.start == "12:00" and c.end == "16:00" for c in u30_cands)  # the 4h afternoon block survives
    assert any(c.start == "08:00" and c.end == "09:00" for c in u30_cands)  # so does the 1h slot before the booking
    assert not any(c.room.name == "Grupperom U28" for c in candidates)  # blocked all day


def test_state_roundtrip(tmp_path: Path) -> None:
    state = State(tmp_path / "state.json")
    state.add_booking(date(2026, 11, 16), "Grupperom U30", "1009.186", "08:00", "12:00", "4242")
    state.record_run({"result": "ok"})
    again = State(tmp_path / "state.json")
    assert again.bookings[0]["id"] == "4242"
    assert again.upcoming(date(2026, 11, 1))[0]["room"] == "Grupperom U30"
    assert again.upcoming(date(2026, 12, 1)) == []
