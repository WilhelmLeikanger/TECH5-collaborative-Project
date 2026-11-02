"""Precise timing and the retry loop that fires at the release instant.

Timing model
------------
The server opens the new day at ``release.time`` *server time*.  We estimate
the offset between our clock and the server's from the HTTP ``Date`` header
(tick detection gives ~100 ms accuracy), then fire the first attempt
``lead_ms`` before the corrected instant.  While the server answers "not open
yet" we retry every ``fast_retry_ms`` for ``burst_seconds``, then every
``slow_retry_s`` until ``max_wait_seconds``.  A rejected slot ("occupied")
moves on to the next candidate immediately; a "too long" rejection drops every
candidate of that length or longer; a quota rejection stops the run.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from .config import Config, parse_hhmm
from .strategy import Candidate, drop_conflicting
from .timeedit import NotLoggedIn, Outcome, ReserveResult, TimeEditClient

log = logging.getLogger("te_bot.sniper")


@dataclass
class ClockSync:
    offset_s: float  # server - local (seconds)
    uncertainty_s: float
    rtt_ms: float
    samples: int
    method: str

    def describe(self) -> str:
        return (
            f"server clock is {self.offset_s:+.3f}s vs local (±{self.uncertainty_s:.3f}s, "
            f"rtt {self.rtt_ms:.0f}ms, {self.samples} samples, {self.method})"
        )


def estimate_offset(client: TimeEditClient, max_samples: int = 30, interval_s: float = 0.1) -> ClockSync:
    """Estimate ``server - local`` by watching the ``Date`` header tick over."""
    prev: tuple[float, float, float, datetime] | None = None  # sent_wall, recv_wall, rtt, server
    rtts: list[float] = []
    fallback: ClockSync | None = None
    for i in range(max_samples):
        wall_before = time.time()
        sent, received, server_date = client.ping()
        wall_after = time.time()
        rtt = received - sent
        rtts.append(rtt)
        if server_date is None:
            break
        server_epoch = server_date.timestamp()
        mid = (wall_before + wall_after) / 2.0
        if fallback is None:
            fallback = ClockSync(server_epoch + 0.5 - mid, 0.5 + rtt / 2, rtt * 1000, 1, "single-sample")
        if prev is not None and server_epoch > prev[3].timestamp():
            # The server's second ticked between the previous response and this
            # one.  The tick happened somewhere between those two generation
            # instants; take the midpoint.
            prev_gen = prev[0] + prev[2] / 2.0
            this_gen = wall_before + rtt / 2.0
            boundary_local = (prev_gen + this_gen) / 2.0
            uncertainty = max(0.02, (this_gen - prev_gen) / 2.0)
            return ClockSync(server_epoch - boundary_local, uncertainty, sum(rtts) / len(rtts) * 1000, i + 1, "tick")
        prev = (wall_before, wall_after, rtt, server_date)
        time.sleep(interval_s)
    if fallback is None:
        raise RuntimeError("Server sent no Date header; cannot sync clock")
    fallback.samples = len(rtts)
    fallback.rtt_ms = sum(rtts) / len(rtts) * 1000
    return fallback


def release_instant(cfg: Config, on_day: date) -> datetime:
    """Local (timezone-aware) datetime of the release on ``on_day``."""
    tz = ZoneInfo(cfg.timeedit.timezone)
    minutes = parse_hhmm(cfg.release.time)
    return datetime(on_day.year, on_day.month, on_day.day, minutes // 60, minutes % 60, tzinfo=tz)


def next_release(cfg: Config, now: datetime | None = None) -> datetime:
    tz = ZoneInfo(cfg.timeedit.timezone)
    now = now or datetime.now(tz)
    candidate = release_instant(cfg, now.date())
    if candidate <= now:
        candidate = release_instant(cfg, now.date() + timedelta(days=1))
    return candidate


def sleep_until(target_epoch: float, now_fn: Callable[[], float] = time.time, sleep_fn: Callable[[float], None] = time.sleep) -> None:
    """Sleep until ``target_epoch`` (wall clock), spinning for the last 15 ms."""
    while True:
        remaining = target_epoch - now_fn()
        if remaining <= 0:
            return
        if remaining > 0.5:
            sleep_fn(min(remaining - 0.25, 30.0))
        elif remaining > 0.015:
            sleep_fn(remaining - 0.01)
        else:  # spin for precision
            while now_fn() < target_epoch:
                pass
            return


@dataclass
class Attempt:
    at: float
    candidate: Candidate
    result: ReserveResult

    def as_dict(self) -> dict:
        return {
            "at": datetime.fromtimestamp(self.at).isoformat(timespec="milliseconds"),
            "room": self.candidate.room.name,
            "object": self.candidate.room.object_id,
            "start": self.candidate.start,
            "end": self.candidate.end,
            "outcome": self.result.outcome.value,
            "status": self.result.status,
            "elapsed_ms": round(self.result.elapsed_ms, 1),
            "message": self.result.message[:200],
        }


@dataclass
class SnipeReport:
    day: date
    booked: list[tuple[Candidate, ReserveResult]] = field(default_factory=list)
    attempts: list[Attempt] = field(default_factory=list)
    stop_reason: str = ""

    @property
    def success(self) -> bool:
        return bool(self.booked)

    def summary(self) -> str:
        if self.booked:
            parts = [f"{c.describe()} (id {r.reservation_id})" for c, r in self.booked]
            return f"BOOKED {self.day:%a %d.%m}: " + "; ".join(parts)
        return f"NO BOOKING for {self.day:%a %d.%m} after {len(self.attempts)} attempts — {self.stop_reason}"


def snipe(
    client: TimeEditClient,
    candidates: list[Candidate],
    day: date,
    cfg: Config,
    fire_at: float,
    fields: dict[str, str] | None = None,
    blocks: int = 1,
    now_fn: Callable[[], float] = time.time,
    sleep_fn: Callable[[float], None] = time.sleep,
    relogin: Optional[Callable[[], bool]] = None,
    dry_run: bool = False,
) -> SnipeReport:
    """Fire reservation attempts starting at ``fire_at`` (epoch seconds)."""
    release = cfg.release
    report = SnipeReport(day=day)
    if not candidates:
        report.stop_reason = "no candidates"
        return report
    queue = list(candidates)

    # Fetch the CSRF token shortly before firing so it is fresh but ready.
    csrf: str | None = None
    try:
        csrf = client.get_csrf()
        log.info("CSRF token ready")
    except NotLoggedIn:
        if relogin and relogin():
            csrf = client.get_csrf()
        else:
            report.stop_reason = "not logged in"
            return report
    except Exception as exc:  # noqa: BLE001 - keep going without a token
        log.warning("Could not fetch CSRF token (%s); will send without it", exc)

    wait = fire_at - now_fn()
    log.info("Waiting %.3fs to fire for %s with %d candidates (first: %s)", max(0.0, wait), day, len(queue), queue[0].describe())
    sleep_until(fire_at, now_fn, sleep_fn)

    burst_end = fire_at + release.burst_seconds
    deadline = fire_at + release.max_wait_seconds
    idx = 0
    not_open_retries = 0
    unknown_retries = 0
    relogin_done = False
    while queue and idx < len(queue):
        now = now_fn()
        if now > deadline:
            report.stop_reason = f"gave up after {release.max_wait_seconds}s"
            break
        cand = queue[idx]
        if dry_run:
            log.info("[dry-run] would POST reserve %s on %s", cand.describe(), day)
            report.stop_reason = "dry run"
            break
        result = client.reserve(cand.room.object_id, day, cand.start, cand.end, csrf, fields, release.min_request_gap_ms)
        report.attempts.append(Attempt(now, cand, result))
        log.info(
            "%s → %s in %.0fms: %s",
            cand.describe(),
            result.outcome.value,
            result.elapsed_ms,
            result.message[:160],
        )
        outcome = result.outcome
        if outcome is Outcome.SUCCESS:
            report.booked.append((cand, result))
            if len(report.booked) >= blocks:
                report.stop_reason = "done"
                break
            queue = drop_conflicting(queue, cand)
            idx = 0
            continue
        if outcome is Outcome.NOT_OPEN:
            not_open_retries += 1
            interval = release.fast_retry_ms / 1000.0 if now_fn() < burst_end else release.slow_retry_s
            sleep_fn(interval)
            continue
        if outcome is Outcome.OCCUPIED:
            idx += 1
            continue
        if outcome is Outcome.LENGTH:
            too_long = cand.hours
            queue = [c for c in queue if c.hours < too_long]
            idx = 0
            log.info("Dropping candidates of %gh or longer; %d left", too_long, len(queue))
            continue
        if outcome is Outcome.QUOTA:
            report.stop_reason = f"quota: {result.message[:120]}"
            break
        if outcome is Outcome.NOT_LOGGED_IN:
            if relogin and not relogin_done:
                relogin_done = True
                log.warning("Session expired mid-run; trying to log in again")
                if relogin():
                    try:
                        csrf = client.get_csrf()
                    except Exception:  # noqa: BLE001
                        pass
                    continue
            report.stop_reason = f"not logged in: {result.message[:120]}"
            break
        if outcome is Outcome.CSRF:
            try:
                csrf = client.get_csrf()
                log.info("Refreshed CSRF token")
                continue
            except Exception as exc:  # noqa: BLE001
                report.stop_reason = f"csrf: {exc}"
                break
        # UNKNOWN: retry a couple of times (maybe transient), then move on.
        unknown_retries += 1
        if unknown_retries % 3 == 0:
            idx += 1
        else:
            if unknown_retries == 1:
                try:
                    csrf = client.get_csrf()
                except Exception:  # noqa: BLE001
                    pass
            sleep_fn(release.fast_retry_ms / 1000.0)
    else:
        if not report.stop_reason:
            report.stop_reason = "all candidates rejected" if report.attempts else "no candidates"
    if not report.stop_reason:
        report.stop_reason = "done"
    return report
