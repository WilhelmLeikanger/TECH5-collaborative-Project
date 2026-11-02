"""Command line interface: ``python -m te_bot <command>``."""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import __version__
from .auth import (
    automated_feide_login,
    credentials_from_env,
    describe_session,
    ensure_session,
    interactive_login,
    load_storage_state,
    relogin_callback,
)
from .config import Config, ConfigError, load_config
from .discover import default_fields_from_discovery, discover, load_discovered, rooms_from_discovered
from .learn import describe_template, learn, load_template
from .schedule_parser import parse_grid
from .sniper import estimate_offset, next_release, release_instant, sleep_until, snipe
from .state import State, setup_logging
from .strategy import (
    Candidate,
    Room,
    build_candidates,
    quota_remaining,
    reservations_in_week,
    resolve_rooms,
    target_date,
    weekday_enabled,
)
from .timeedit import NotLoggedIn, TimeEditClient

EXAMPLE_CONFIG = Path(__file__).resolve().parent.parent / "config.example.toml"


# -- helpers -----------------------------------------------------------------
def make_client(cfg: Config) -> TimeEditClient:
    client = TimeEditClient(cfg.timeedit, load_storage_state(cfg), load_template(cfg))
    if not client.template:
        _fields, extra = default_fields_from_discovery(load_discovered(cfg))
        client.extra_objects = extra
    return client


def reservation_fields(cfg: Config) -> dict[str, str] | None:
    """Form fields to send: discovered defaults overridden by config ``strategy.fields``."""
    defaults, _extra = default_fields_from_discovery(load_discovered(cfg))
    merged = {**defaults, **cfg.strategy.fields}
    return merged or None


def notify(cfg: Config, title: str, message: str) -> None:
    """Best-effort desktop notification (never raises)."""
    try:
        system = platform.system()
        if system == "Darwin":
            subprocess.run(["osascript", "-e", f'display notification "{message}" with title "{title}"'], timeout=5, check=False)
        elif system == "Linux" and shutil.which("notify-send"):
            subprocess.run(["notify-send", title, message], timeout=5, check=False)
        elif system == "Windows":
            script = (
                "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null;"
                f"$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(1);"
                f"$t.GetElementsByTagName('text')[0].AppendChild($t.CreateTextNode('{title}: {message}')) | Out-Null;"
                "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('te_bot').Show($t)"
            )
            subprocess.run(["powershell", "-NoProfile", "-Command", script], timeout=10, check=False)
    except Exception:  # noqa: BLE001
        pass
    print("\a", end="", flush=True)


def gather_rooms(cfg: Config, client: TimeEditClient, log: Any, refresh: bool = False) -> list[Room]:
    discovered = None if refresh else load_discovered(cfg)
    rooms = rooms_from_discovered(discovered)
    if not rooms:
        log.info("No discovered rooms yet; running discovery")
        discovered = discover(cfg, client)
        rooms = rooms_from_discovered(discovered)
    return resolve_rooms(cfg.strategy, cfg.timeedit, rooms)


def availability(cfg: Config, client: TimeEditClient, rooms: list[Room], day: date, log: Any) -> dict[str, list[tuple[int, int]]] | None:
    """Busy intervals per room on ``day`` from the week grid, or ``None`` if unknown."""
    if not rooms:
        return None
    discovered = load_discovered(cfg) or {}
    try:
        html = client.grid_html([r.object_id for r in rooms[:60]], day, week_form=bool(discovered.get("grid_week_form")))
        grid = parse_grid(html)
        if day.isoformat() not in grid.days:
            html = client.grid_html([r.object_id for r in rooms[:60]], day, week_form=not discovered.get("grid_week_form"))
            grid = parse_grid(html)
        if day.isoformat() in grid.days:
            busy = grid.busy(day.isoformat())
            count = sum(len(v) for v in busy.values())
            log.info("Grid for %s parsed: %d rooms, %d existing bookings", day, len(grid.rooms), count)
            return busy
        log.info("Grid for %s could not be parsed; skipping availability pre-check", day)
    except NotLoggedIn:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("Availability check failed (%s); continuing without it", exc)
    return None


def all_reservations(cfg: Config, client: TimeEditClient, state: State, log: Any) -> list[dict[str, Any]]:
    """Union of TimeEdit's ``my.json`` list and the bot's own record."""
    known: list[dict[str, Any]] = []
    try:
        known = client.my_reservations()
        log.info("TimeEdit lists %d reservation(s) for this user", len(known))
    except NotLoggedIn:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not read my.json (%s); using local state only", exc)
    seen = {(r.get("date"), (r.get("start") or "")[:5], r.get("room")) for r in known}
    for booking in state.upcoming():
        key = (booking.get("date"), booking.get("start"), booking.get("room"))
        if key not in seen:
            known.append(booking)
    return known


def plan_for(cfg: Config, client: TimeEditClient, state: State, day: date, log: Any) -> tuple[list[Candidate], dict[str, Any]]:
    rooms = gather_rooms(cfg, client, log)
    info: dict[str, Any] = {"day": day, "rooms": rooms, "weekday_ok": weekday_enabled(cfg.strategy, day)}
    reservations = all_reservations(cfg, client, state, log)
    info["week_reservations"] = reservations_in_week(day, reservations)
    info["quota_left"] = quota_remaining(cfg.strategy, day, reservations)
    busy = availability(cfg, client, rooms, day, log)
    info["busy_known"] = busy is not None
    candidates = build_candidates(cfg.strategy, rooms, busy)
    return candidates, info


def print_plan(cfg: Config, candidates: list[Candidate], info: dict[str, Any]) -> None:
    day = info["day"]
    print(f"\nTarget day: {day:%A %d.%m.%Y} (weekday enabled: {info['weekday_ok']})")
    print(f"Rooms resolved: {len(info['rooms'])} → " + ", ".join(r.name for r in info["rooms"][:8]) + (" …" if len(info["rooms"]) > 8 else ""))
    week = info["week_reservations"]
    print(f"Reservations already in that ISO week: {len(week)} (quota {cfg.strategy.weekly_quota}, left {info['quota_left']})")
    for res in week:
        print(f"   - {res.get('date')} {res.get('start')}-{res.get('end')} {res.get('room') or ''}")
    print(f"Availability pre-check: {'used' if info['busy_known'] else 'not available (will just try)'}")
    print(f"Attempt order ({len(candidates)} candidates):")
    for i, cand in enumerate(candidates[:25], 1):
        print(f"  {i:2d}. {cand.describe()}")
    if len(candidates) > 25:
        print(f"  … and {len(candidates) - 25} more")


# -- commands ----------------------------------------------------------------
def cmd_init(cfg: Config, args: argparse.Namespace, log: Any) -> int:
    cfg.ensure_dirs()
    target = cfg.home / "config.toml"
    if target.exists() and not args.force:
        print(f"Config already exists: {target}")
        return 0
    shutil.copyfile(EXAMPLE_CONFIG, target)
    print(f"Wrote {target}\nEdit it (rooms, windows, weekdays), then run: python -m te_bot login")
    return 0


def cmd_login(cfg: Config, args: argparse.Namespace, log: Any) -> int:
    creds = credentials_from_env(cfg)
    if args.auto or cfg.auth.method == "feide":
        if not creds:
            print(f"Set {cfg.auth.username_env} and {cfg.auth.password_env} in the environment for automated login")
            return 2
        automated_feide_login(cfg, *creds, headless=not args.headed)
    else:
        interactive_login(cfg)
    client = make_client(cfg)
    ok = client.is_logged_in()
    print(f"Logged in: {ok}  cookies: {', '.join(client.cookie_names())}")
    if ok:
        for res in client.my_reservations():
            print(f"  reservation {res.get('id')}: {res.get('date')} {res.get('start')}-{res.get('end')} {res.get('room') or ''}")
    return 0 if ok else 1


def cmd_check(cfg: Config, args: argparse.Namespace, log: Any) -> int:
    print(f"Config: {cfg.source or '(defaults)'}  home: {cfg.home}")
    print(f"Reserve page: {cfg.timeedit.reserve_url} (sid {cfg.timeedit.sid})")
    print(f"Session: {describe_session(cfg)}")
    client = make_client(cfg)
    try:
        ok = client.is_logged_in()
    except Exception as exc:  # noqa: BLE001
        print(f"Session check failed: {exc}")
        return 1
    print(f"Logged in: {ok}")
    if ok:
        try:
            for res in client.my_reservations():
                print(f"  reservation {res.get('id')}: {res.get('date')} {res.get('start')}-{res.get('end')} {res.get('room') or ''}")
        except Exception as exc:  # noqa: BLE001
            print(f"  (could not list reservations: {exc})")
    template = load_template(cfg)
    print(f"Template: {'present — ' + str(cfg.template_file) if template else 'none (run learn, or rely on defaults)'}")
    discovered = load_discovered(cfg)
    print(f"Discovered rooms: {len(discovered.get('rooms', [])) if discovered else 0}")
    tz = ZoneInfo(cfg.timeedit.timezone)
    nxt = next_release(cfg)
    print(f"Next release: {nxt:%Y-%m-%d %H:%M %Z} → opens {target_date(cfg.release, nxt):%A %d.%m.%Y}; now {datetime.now(tz):%H:%M:%S %Z}")
    return 0 if ok else 1


def cmd_discover(cfg: Config, args: argparse.Namespace, log: Any) -> int:
    client = make_client(cfg)
    if not ensure_session(cfg, client, allow_interactive=not args.no_interactive):
        print("Not logged in")
        return 1
    result = discover(cfg, client)
    print(f"sid: {result.get('sid')}  room type: {result.get('room_type')}  rooms: {len(result.get('rooms', []))}  ({result.get('rooms_source', '-')})")
    for room in result.get("rooms", []):
        print(f"  {room['id']}.{room['type']}  {room['name']}")
    fields = [f["name"] for f in result.get("form_fields", [])]
    if fields:
        print("Reservation form fields: " + ", ".join(fields))
    for note in result.get("notes", []):
        print(f"NOTE: {note}")
    return 0


def cmd_learn(cfg: Config, args: argparse.Namespace, log: Any) -> int:
    template = learn(cfg, timeout_s=args.timeout)
    print("\nLearned reservation template:")
    print(describe_template(template))
    print(f"\nSaved to {cfg.template_file}. Cancel the test reservation in TimeEdit if you do not need it.")
    return 0


def cmd_plan(cfg: Config, args: argparse.Namespace, log: Any) -> int:
    tz = ZoneInfo(cfg.timeedit.timezone)
    day = args.date or target_date(cfg.release, next_release(cfg))
    client = make_client(cfg)
    if not ensure_session(cfg, client, allow_interactive=not args.no_interactive):
        print("Not logged in")
        return 1
    state = State(cfg.state_file)
    candidates, info = plan_for(cfg, client, state, day, log)
    print_plan(cfg, candidates, info)
    print(f"\nNow: {datetime.now(tz):%Y-%m-%d %H:%M:%S %Z}; next release {next_release(cfg):%Y-%m-%d %H:%M}")
    return 0


def run_once(cfg: Config, args: argparse.Namespace, log: Any, state: State) -> dict[str, Any]:
    tz = ZoneInfo(cfg.timeedit.timezone)
    now = datetime.now(tz)
    if args.now:
        release_dt = now + timedelta(seconds=2)
        day = args.date or target_date(cfg.release, now)
    else:
        release_dt = release_instant(cfg, now.date())
        if now > release_dt + timedelta(seconds=cfg.release.max_wait_seconds):
            release_dt = release_instant(cfg, now.date() + timedelta(days=1))
        day = args.date or target_date(cfg.release, release_dt)
    summary: dict[str, Any] = {"day": day.isoformat(), "release": release_dt.isoformat(), "started": now.isoformat(timespec="seconds")}
    log.info("Release %s → target day %s (%s)", release_dt.strftime("%Y-%m-%d %H:%M %Z"), day, day.strftime("%A"))

    if not weekday_enabled(cfg.strategy, day):
        log.info("%s is not an enabled weekday (%s); nothing to do tonight", day.strftime("%A"), ", ".join(cfg.strategy.weekdays))
        summary["result"] = "skipped: weekday"
        return summary

    prepare_at = release_dt - timedelta(minutes=cfg.release.prepare_minutes)
    if datetime.now(tz) < prepare_at:
        log.info("Sleeping until %s to prepare", prepare_at.strftime("%H:%M:%S"))
        sleep_until(prepare_at.timestamp())

    client = make_client(cfg)
    interactive = not args.no_interactive and sys.stdin.isatty()
    if not ensure_session(cfg, client, allow_interactive=interactive):
        log.error("No valid session and could not log in — run 'python -m te_bot login'")
        notify(cfg, "te_bot", "Not logged in — booking skipped")
        summary["result"] = "failed: not logged in"
        return summary

    candidates, info = plan_for(cfg, client, state, day, log)
    print_plan(cfg, candidates, info)
    blocks = cfg.strategy.blocks_per_day
    if info["quota_left"] <= 0:
        log.warning(
            "Weekly quota looks used up (%d/%d in that week) — trying anyway; the server has the final say",
            len(info["week_reservations"]),
            cfg.strategy.weekly_quota,
        )
    else:
        blocks = min(blocks, info["quota_left"])
    if not candidates:
        log.warning("No candidates (rooms unresolved or everything busy)")
        summary["result"] = "skipped: no candidates"
        return summary

    fire_at = release_dt.timestamp() - cfg.release.lead_ms / 1000.0
    if cfg.release.clock_sync and not args.now:
        sync_at = release_dt - timedelta(seconds=45)
        if datetime.now(tz) < sync_at:
            sleep_until(sync_at.timestamp())
        try:
            sync = estimate_offset(client)
            log.info("Clock sync: %s", sync.describe())
            fire_at -= sync.offset_s
            summary["clock_offset_s"] = round(sync.offset_s, 3)
        except Exception as exc:  # noqa: BLE001
            log.warning("Clock sync failed (%s); using local clock", exc)

    report = snipe(
        client,
        candidates,
        day,
        cfg,
        fire_at,
        fields=reservation_fields(cfg),
        blocks=blocks,
        relogin=relogin_callback(cfg, client, interactive),
        dry_run=args.dry_run,
    )
    for cand, result in report.booked:
        state.add_booking(day, cand.room.name, cand.room.object_id, cand.start, cand.end, result.reservation_id)
    summary["result"] = report.summary()
    summary["attempts"] = len(report.attempts)
    summary["booked"] = [c.describe() for c, _ in report.booked]
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    (cfg.log_dir / f"run-{day.isoformat()}.json").write_text(
        json.dumps({**summary, "attempt_log": [a.as_dict() for a in report.attempts]}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    log.info(report.summary())
    notify(cfg, "te_bot", report.summary())
    return summary


def cmd_run(cfg: Config, args: argparse.Namespace, log: Any) -> int:
    state = State(cfg.state_file)
    while True:
        try:
            summary = run_once(cfg, args, log, state)
            state.record_run(summary)
        except NotLoggedIn as exc:
            log.error("Not logged in: %s", exc)
            state.record_run({"result": f"failed: {exc}", "started": datetime.now().isoformat(timespec='seconds')})
        if args.once or args.now:
            return 0
        tz = ZoneInfo(cfg.timeedit.timezone)
        nxt = next_release(cfg, datetime.now(tz) + timedelta(minutes=1))
        log.info("Next release at %s", nxt.strftime("%Y-%m-%d %H:%M %Z"))
        sleep_until((nxt - timedelta(minutes=cfg.release.prepare_minutes + 1)).timestamp())


def cmd_status(cfg: Config, args: argparse.Namespace, log: Any) -> int:
    state = State(cfg.state_file)
    print(f"State file: {cfg.state_file}")
    upcoming = state.upcoming()
    print(f"Upcoming bookings made by the bot: {len(upcoming)}")
    for booking in upcoming:
        print(f"  {booking['date']} {booking['start']}-{booking['end']} {booking['room']} (id {booking.get('id')})")
    print("Recent runs:")
    for run in state.data.get("runs", [])[-10:]:
        print(f"  {run.get('started', '?')[:16]}  {run.get('day', '')}  {run.get('result', '')}")
    return 0


def cmd_cancel(cfg: Config, args: argparse.Namespace, log: Any) -> int:
    client = make_client(cfg)
    if not ensure_session(cfg, client, allow_interactive=not args.no_interactive):
        print("Not logged in")
        return 1
    ok, text = client.cancel(args.reservation_id)
    print(("Cancelled: " if ok else "Cancel failed: ") + text)
    return 0 if ok else 1


def cmd_mock(cfg: Config, args: argparse.Namespace, log: Any) -> int:
    from .mock_server import serve

    serve(port=args.port, release_in_s=args.release_in)
    return 0


def cmd_selftest(cfg: Config, args: argparse.Namespace, log: Any) -> int:
    """Run the whole pipeline against the in-process mock server."""
    from .mock_server import MockTimeEdit, start_server
    from .auth import save_storage_state

    release_in = args.release_in
    mock = MockTimeEdit(release_at=time.time() + release_in, blocked_rooms=("Grupperom U28",))
    server, base_url = start_server(mock)
    try:
        test_home = cfg.home / "selftest"
        test_cfg = Config(home=test_home)
        test_cfg.timeedit.base_url = base_url
        test_cfg.strategy.rooms = ["Grupperom U28", "Grupperom U30"]
        test_cfg.strategy.weekdays = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        test_cfg.release.prepare_minutes = 0
        test_cfg.release.lead_ms = 30
        test_cfg.ensure_dirs()
        save_storage_state(test_cfg, {"cookies": [{"name": mock.cookie_name, "value": mock.cookie_value, "domain": "127.0.0.1", "path": "/"}]})
        if test_cfg.state_file.exists():
            test_cfg.state_file.unlink()
        state = State(test_cfg.state_file)
        tz = ZoneInfo(test_cfg.timeedit.timezone)
        release_dt = datetime.fromtimestamp(mock.release_at or time.time(), tz)
        print(f"Mock server at {base_url}; release in {release_in:.0f}s at {release_dt:%H:%M:%S} — U28 is blocked, so U30 should win.")
        day = date.today() + timedelta(days=test_cfg.release.days_ahead)
        client = make_client(test_cfg)
        candidates, info = plan_for(test_cfg, client, state, day, log)
        print_plan(test_cfg, candidates, info)
        sync = estimate_offset(client)
        print("Clock sync:", sync.describe())
        fire_at = (mock.release_at or time.time()) - sync.offset_s - test_cfg.release.lead_ms / 1000.0
        report = snipe(client, candidates, day, test_cfg, fire_at)
        print(report.summary())
        not_open = sum(1 for a in report.attempts if a.result.outcome.value == "not_open")
        print(f"attempts: {len(report.attempts)} (not-open retries: {not_open}); mock saw {mock.counters['reserve']} reserve POSTs")
        ok = report.success and report.booked[0][0].room.name == "Grupperom U30" and report.booked[0][0].hours == test_cfg.strategy.max_hours
        print("SELFTEST", "PASSED" if ok else "FAILED")
        return 0 if ok else 1
    finally:
        server.shutdown()


# -- entry point -------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="te_bot", description="Precise, polite TimeEdit room sniper (NHH)")
    parser.add_argument("--config", help="path to config.toml (default: ~/.timeedit-bot/config.toml)")
    parser.add_argument("--home", help="data directory (default: ~/.timeedit-bot)")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=f"te_bot {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="write a config.toml template to the data directory")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("login", help="log in to TimeEdit (Feide) in a browser and save the session")
    p.add_argument("--auto", action="store_true", help="automated Feide login using FEIDE_USERNAME/FEIDE_PASSWORD")
    p.add_argument("--headed", action="store_true", help="show the browser during automated login")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("check", help="show session, template and next release")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("discover", help="detect rooms, ids and form fields from TimeEdit")
    p.add_argument("--no-interactive", action="store_true")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("learn", help="record one manual reservation to learn the exact request")
    p.add_argument("--timeout", type=int, default=900)
    p.set_defaults(func=cmd_learn)

    p = sub.add_parser("plan", help="show what the bot would try for the next release")
    p.add_argument("--date", type=date.fromisoformat, help="target day (YYYY-MM-DD)")
    p.add_argument("--no-interactive", action="store_true")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("run", help="wait for the release and book (loops daily unless --once)")
    p.add_argument("--once", action="store_true", help="handle one release, then exit")
    p.add_argument("--now", action="store_true", help="fire immediately (no waiting, no clock sync)")
    p.add_argument("--date", type=date.fromisoformat, help="book this day instead of today+days_ahead")
    p.add_argument("--dry-run", action="store_true", help="do everything except the reservation POST")
    p.add_argument("--no-interactive", action="store_true", help="never open a browser window")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("status", help="show bookings made by the bot and recent runs")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("cancel", help="cancel a reservation by id")
    p.add_argument("reservation_id")
    p.add_argument("--no-interactive", action="store_true")
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("mock", help="run a fake TimeEdit server for experiments")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--release-in", type=float, default=60.0)
    p.set_defaults(func=cmd_mock)

    p = sub.add_parser("selftest", help="exercise the whole pipeline against the built-in mock")
    p.add_argument("--release-in", type=float, default=8.0)
    p.set_defaults(func=cmd_selftest)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config, Path(args.home).expanduser() if args.home else None)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    log = setup_logging(cfg, args.verbose)
    try:
        return int(args.func(cfg, args, log) or 0)
    except KeyboardInterrupt:
        print("\nInterrupted")
        return 130
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        log.error("%s", exc, exc_info=args.verbose)
        return 1
