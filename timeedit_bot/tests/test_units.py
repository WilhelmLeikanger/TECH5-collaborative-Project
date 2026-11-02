"""Unit tests for the pure-Python parts of te_bot (no network, no browser)."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from te_bot import scramble
from te_bot.config import Config, ConfigError, from_dict, parse_hhmm
from te_bot.schedule_parser import parse_grid
from te_bot.sniper import next_release, release_instant, sleep_until
from te_bot.strategy import (
    Candidate,
    Room,
    build_candidates,
    candidate_windows,
    drop_conflicting,
    quota_remaining,
    resolve_rooms,
    target_date,
    weekday_enabled,
)
from te_bot.timeedit import Outcome, TEObject, TimeEditClient, classify, extract_reservation_id, parse_objects


# -- scramble ----------------------------------------------------------------
def test_scramble_decodes_known_keys() -> None:
    assert scramble.decode_key("ri1Q56.html") == "h=t&sid=12"
    assert scramble.sid_from_key("ri1Q56.html") == 12
    assert scramble.sid_from_key("ri1Q8.html") == 4  # Chalmers student page
    assert scramble.sid_from_key("ri1Q5008.html") == 1004  # Chalmers staff page
    assert scramble.encode_query("h=t&sid=12") == "1Q56"


# -- config ------------------------------------------------------------------
def test_config_defaults_are_valid(tmp_path: Path) -> None:
    cfg = from_dict({}, home=tmp_path)
    assert cfg.timeedit.reserve_url.endswith("/nhh/web/schedule/ri1Q56.html")
    assert cfg.timeedit.origin == "https://cloud.timeedit.net"
    assert cfg.timeedit.web_root == "https://cloud.timeedit.net/nhh/web"
    assert cfg.timeedit.org == "nhh"
    assert cfg.strategy.weekdays == ["mon", "tue", "wed", "thu"]


def test_config_rejects_bad_values(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        from_dict({"strategy": {"windows": ["12:00-08:00"]}}, home=tmp_path)
    with pytest.raises(ConfigError):
        from_dict({"strategy": {"order": "random"}}, home=tmp_path)
    with pytest.raises(ConfigError):
        from_dict({"nonsense": {}}, home=tmp_path)
    with pytest.raises(ConfigError):
        from_dict({"release": {"time": "9pm"}}, home=tmp_path)
    assert parse_hhmm("21:00") == 1260


# -- classification ----------------------------------------------------------
@pytest.mark.parametrize(
    "status,text,location,expected",
    [
        (302, "", "/nhh/web/schedule/ri1Q56.html?h=t&sid=12&id=123456&step=3&newres=t", Outcome.SUCCESS),
        (200, "https://cloud.timeedit.net/x/r.html?sid=12&h=t&id=777&step=3&newres=t", None, Outcome.SUCCESS),
        (409, "The reservation could not be made. The reservation is outside the date limits.", None, Outcome.NOT_OPEN),
        (409, "Bokningen kunde ej genomföras.  Bokningen är utanför datumgränserna", None, Outcome.NOT_OPEN),
        (409, "Reservasjonen må være innenfor dato- og klokkeslettgrensene", None, Outcome.NOT_OPEN),
        (409, "The time is occupied because the object Grupperom U28 is already reserved.", None, Outcome.OCCUPIED),
        (409, "Bokningen kunde ej genomföras.  Tiden är upptagen eftersom objektet", None, Outcome.OCCUPIED),
        (409, "Användarens maximala bokningslängd har överskridits", None, Outcome.LENGTH),
        (409, "The user's maximum booking length has been exceeded.", None, Outcome.LENGTH),
        (409, "The maximum number of reservations per week has been exceeded.", None, Outcome.QUOTA),
        (409, "You already have 4 reservations this week", None, Outcome.QUOTA),
        (412, "<html><form><input type=password></form>Not logged in</html>", None, Outcome.NOT_LOGGED_IN),
        (409, "Invalid CSTT token", None, Outcome.CSRF),
        (500, "Internal error", None, Outcome.UNKNOWN),
    ],
)
def test_classify(status: int, text: str, location: str | None, expected: Outcome) -> None:
    outcome, _ = classify(status, text, location)
    assert outcome is expected


def test_extract_reservation_id() -> None:
    assert extract_reservation_id("/x/ri1Q56.html?h=t&sid=12&id=4242&step=3") == "4242"
    assert extract_reservation_id(None) is None


# -- objects.json parsing -----------------------------------------------------
def test_parse_objects_handles_known_shapes() -> None:
    chalmers = '{"objects":[{"id":"501","idAndType":"501.186","fields":{"Lokalsignatur":"Grupperom U28","Type":"Grupperom"}}],"hasMore":false}'
    rooms = parse_objects(chalmers)
    assert rooms[0].object_id == "501.186" and rooms[0].name == "Grupperom U28"
    liu = '{"records":[{"id":"77.195","fields":[{"values":["Grupperom 101"]}]}],"ids":["77.195"]}'
    rooms = parse_objects(liu)
    assert rooms[0].object_id == "77.195" and rooms[0].name == "Grupperom 101"
    assert parse_objects('"No search results"') == []
    assert parse_objects("") == []


# -- strategy ----------------------------------------------------------------
def _cfg(**strategy) -> Config:
    data = {"strategy": strategy} if strategy else {}
    return from_dict(data, home=Path("/tmp/te_bot_test_home"))


def test_candidate_windows_longest_first() -> None:
    cfg = _cfg(windows=["08:00-12:00", "12:00-16:00"], max_hours=4, min_hours=2)
    windows = candidate_windows(cfg.strategy)
    assert windows[:2] == [(8 * 60, 12 * 60), (12 * 60, 16 * 60)]
    # then the 3-hour blocks inside each window, then 2-hour blocks
    assert (8 * 60, 11 * 60) in windows and (9 * 60, 12 * 60) in windows
    assert windows[-1] == (14 * 60, 16 * 60)
    lengths = [e - s for s, e in windows]
    assert lengths == sorted(lengths, reverse=True)


def test_resolve_rooms_priority_short_names_and_fallback() -> None:
    cfg = _cfg(rooms=["U30", "Grupperom U28", "Does not exist"], exclude_rooms=["Grupperom U21"], any_room_fallback=True)
    known = [
        TEObject("1", "186", "Grupperom U21"),
        TEObject("2", "186", "Grupperom U28"),
        TEObject("3", "186", "Grupperom U30"),
        TEObject("4", "186", "Grupperom 101"),
        TEObject("5", "186", "Auditorium A"),
    ]
    rooms = resolve_rooms(cfg.strategy, cfg.timeedit, known)
    assert [r.name for r in rooms] == ["Grupperom U30", "Grupperom U28", "Grupperom 101"]


def test_build_candidates_orders_and_filters() -> None:
    cfg = _cfg(rooms=["A", "B"], any_room_fallback=False, windows=["08:00-12:00", "12:00-16:00"], max_hours=4, min_hours=4)
    rooms = [Room("A", "1.186"), Room("B", "2.186")]
    cands = build_candidates(cfg.strategy, rooms)
    assert [c.describe() for c in cands] == [
        "A 08:00-12:00 (4h)",
        "A 12:00-16:00 (4h)",
        "B 08:00-12:00 (4h)",
        "B 12:00-16:00 (4h)",
    ]
    cfg.strategy.order = "window-first"
    cands = build_candidates(cfg.strategy, rooms)
    assert [c.describe() for c in cands][:2] == ["A 08:00-12:00 (4h)", "B 08:00-12:00 (4h)"]
    busy = {"1.186": [(10 * 60, 11 * 60)]}
    cands = build_candidates(cfg.strategy, rooms, busy)
    assert all(not (c.room.name == "A" and c.start == "08:00") for c in cands)
    remaining = drop_conflicting(cands, Candidate(Room("B", "2.186"), 8 * 60, 12 * 60))
    assert all(c.room.name != "B" for c in remaining)
    assert all(c.start_min >= 12 * 60 for c in remaining)


def test_quota_and_dates() -> None:
    cfg = _cfg(weekly_quota=4, weekdays=["mon", "tue"])
    monday = date(2026, 11, 16)
    assert weekday_enabled(cfg.strategy, monday)
    assert not weekday_enabled(cfg.strategy, date(2026, 11, 20))
    reservations = [
        {"date": "2026-11-17", "start": "08:00"},
        {"date": "20261118", "start": "08:00"},
        {"date": "2026-11-23", "start": "08:00"},  # next week
    ]
    assert quota_remaining(cfg.strategy, monday, reservations) == 2
    now = datetime(2026, 11, 2, 21, 0, tzinfo=ZoneInfo("Europe/Oslo"))
    assert target_date(cfg.release, now) == date(2026, 11, 16)


def test_release_instants() -> None:
    cfg = _cfg()
    tz = ZoneInfo("Europe/Oslo")
    before = datetime(2026, 11, 2, 20, 0, tzinfo=tz)
    after = datetime(2026, 11, 2, 21, 30, tzinfo=tz)
    assert release_instant(cfg, before.date()) == datetime(2026, 11, 2, 21, 0, tzinfo=tz)
    assert next_release(cfg, before).date() == date(2026, 11, 2)
    assert next_release(cfg, after).date() == date(2026, 11, 3)


def test_sleep_until_uses_fake_clock() -> None:
    clock = {"t": 100.0}
    slept: list[float] = []

    def now() -> float:
        clock["t"] += 0.001  # a real clock keeps moving while we spin
        return clock["t"]

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock["t"] += seconds

    sleep_until(103.0, now, sleep)
    assert clock["t"] >= 103.0
    assert all(s > 0 for s in slept)


# -- grid parsing --------------------------------------------------------------
GRID = """
<div class="textHTML">Max 4 hours.<br/>Max 4 per week.</div>
<div class="weekDay" data-day="20261116">
  <div class="weekDiv" data-hourHeight="40" style="height: 120px">
    <div class="weekHourLine">
      <div class="hour" data-object="1.186"></div>
      <div class="hour" data-object="2.186"></div>
      <div class="hour" data-object="3.186"></div>
    </div>
    <div class="bookingDiv" title="2026-11-16 08:00 - 10:00, Auditorium C, ID 55" style="top: 40px; height: 40px"></div>
    <div class="bookingDiv" title="2026-11-16 22:00 - 00:00, Late, ID 56" style="top: 81px; height: 40px"></div>
  </div>
</div>
<div class="weekDay" data-day="20261117"><div class="weekDiv" data-object="9.186">
  <div class="bookingDiv" title="2026-11-17 12:00 - 13:30, X, ID 57" style="top: 0px"></div>
</div></div>
"""


def test_parse_grid_rows_and_times() -> None:
    grid = parse_grid(GRID)
    assert grid.days == ["2026-11-16", "2026-11-17"]
    assert grid.rooms == ["1.186", "2.186", "3.186", "9.186"]
    busy = grid.busy("2026-11-16")
    assert busy["1.186"] == []
    assert busy["2.186"] == [(8 * 60, 10 * 60)]
    assert busy["3.186"] == [(22 * 60, 24 * 60)]
    assert grid.busy("2026-11-17")["9.186"] == [(12 * 60, 13 * 60 + 30)]
    assert "Max 4 hours" in grid.rules
    assert parse_grid("<html><body>nothing here</body></html>").days == []


# -- request body ------------------------------------------------------------
def test_build_reserve_body_from_template() -> None:
    cfg = _cfg()
    template = {
        "url": cfg.timeedit.reserve_url,
        "room_object": "1234.186",
        "fields": [
            ["kind", "reserve"],
            ["nocache", "4"],
            ["l", "en_GB"],
            ["o", "1234.186"],
            ["o", "999.190"],
            ["aos", ""],
            ["dates", "20261101"],
            ["starttime", "16:00"],
            ["endtime", "17:00"],
            ["url", cfg.timeedit.reserve_url],
            ["fe2", "Group work"],
            ["CSTT", "1"],
        ],
    }
    client = TimeEditClient(cfg.timeedit, template=template)
    body = dict_multi(client.build_reserve_body("5555.186", date(2026, 11, 16), "08:00", "12:00", "42"))
    assert body["o"] == ["5555.186", "999.190"]
    assert body["dates"] == ["20261116"]
    assert body["starttime"] == ["08:00"] and body["endtime"] == ["12:00"]
    assert body["CSTT"] == ["42"] and body["fe2"] == ["Group work"]
    assert body["kind"] == ["reserve"]


def test_build_reserve_body_default() -> None:
    cfg = _cfg()
    client = TimeEditClient(cfg.timeedit)
    body = dict_multi(client.build_reserve_body("5555.186", date(2026, 11, 16), "08:00", "12:00", "42", {"fe2": "x"}))
    assert body["o"] == ["5555.186"] and body["kind"] == ["reserve"] and body["aos"] == [""]
    assert body["url"] == [cfg.timeedit.reserve_url] and body["fe2"] == ["x"] and body["CSTT"] == ["42"]


def dict_multi(pairs: list[tuple[str, str]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for key, value in pairs:
        out.setdefault(key, []).append(value)
    return out
