"""Booking strategy: which (room, time window) pairs to try, in which order.

NHH's rules (nhh.no → "Group rooms"):

* rooms open for booking 14 days ahead (one new day every evening),
* a reservation may last at most 4 hours,
* each student may hold at most 4 reservations per week.

So the plan each evening is: pick the day that just opened, check the weekly
quota, then try the longest allowed block in the most wanted room, falling
back to other windows, other rooms and finally shorter blocks.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from .config import WEEKDAYS, ReleaseConfig, StrategyConfig, TimeEditConfig, fmt_hhmm
from .timeedit import TEObject

log = logging.getLogger("te_bot.strategy")


@dataclass(frozen=True)
class Room:
    name: str
    object_id: str  # "<id>.<type>"


@dataclass(frozen=True)
class Candidate:
    room: Room
    start_min: int
    end_min: int

    @property
    def start(self) -> str:
        return fmt_hhmm(self.start_min)

    @property
    def end(self) -> str:
        return fmt_hhmm(self.end_min)

    @property
    def hours(self) -> float:
        return (self.end_min - self.start_min) / 60.0

    def describe(self) -> str:
        return f"{self.room.name} {self.start}-{self.end} ({self.hours:g}h)"

    def overlaps(self, start_min: int, end_min: int) -> bool:
        return self.start_min < end_min and start_min < self.end_min


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def _matches(wanted: str, actual: str) -> bool:
    """``"U28"`` matches ``"Grupperom U28"``; exact (normalised) names always match."""
    wanted_n, actual_n = _norm(wanted), _norm(actual)
    if wanted_n == actual_n:
        return True
    tokens = actual_n.split(" ")
    return wanted_n in tokens or actual_n.endswith(" " + wanted_n)


def resolve_rooms(
    strategy: StrategyConfig, te: TimeEditConfig, known: Iterable[TEObject | Room]
) -> list[Room]:
    """Turn the configured room names into concrete TimeEdit objects.

    ``known`` is the list of rooms discovered from TimeEdit.  Names in the
    priority list that cannot be resolved are logged and skipped.
    """
    rooms: list[Room] = []
    for obj in known:
        if isinstance(obj, Room):
            rooms.append(obj)
        else:
            rooms.append(Room(obj.name, obj.object_id))
    excluded = [_norm(x) for x in strategy.exclude_rooms]

    def is_excluded(room: Room) -> bool:
        return any(_matches(x, room.name) for x in excluded)

    ordered: list[Room] = []
    used: set[str] = set()
    for wanted in strategy.rooms:
        hit = next((r for r in rooms if _matches(wanted, r.name) and r.object_id not in used), None)
        if hit is None:
            log.warning("Configured room %r not found among %d known rooms", wanted, len(rooms))
            continue
        if is_excluded(hit):
            continue
        ordered.append(hit)
        used.add(hit.object_id)
    if strategy.any_room_fallback:
        pattern = re.compile(te.room_name_regex)
        for room in rooms:
            if room.object_id in used or is_excluded(room):
                continue
            if pattern.search(room.name):
                ordered.append(room)
                used.add(room.object_id)
    return ordered


def candidate_windows(strategy: StrategyConfig) -> list[tuple[int, int]]:
    """All (start, end) minute pairs to try, longest first, in window priority."""
    windows = strategy.window_minutes()
    step = max(1, int(round(strategy.duration_step_hours * 60)))
    max_len = int(round(strategy.max_hours * 60))
    min_len = int(round(strategy.min_hours * 60))
    durations = [max_len]
    if strategy.fallback_shorter:
        length = max_len - step
        while length >= min_len:
            durations.append(length)
            length -= step
    out: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for length in durations:
        for w_start, w_end in windows:
            if w_end - w_start < length:
                continue
            start = w_start
            while start + length <= w_end:
                pair = (start, start + length)
                if pair not in seen:
                    seen.add(pair)
                    out.append(pair)
                start += step
    return out


def build_candidates(
    strategy: StrategyConfig,
    rooms: list[Room],
    busy: dict[str, list[tuple[int, int]]] | None = None,
) -> list[Candidate]:
    """Combine rooms and windows into an ordered attempt list.

    ``busy`` maps room object ids to already-booked (start, end) minute
    intervals on the target day; candidates overlapping them are dropped.
    Rooms missing from ``busy`` are assumed free (we simply try them).
    """
    windows = candidate_windows(strategy)
    out: list[Candidate] = []
    if strategy.order == "window-first":
        for start, end in windows:
            for room in rooms:
                out.append(Candidate(room, start, end))
    else:
        for room in rooms:
            for start, end in windows:
                out.append(Candidate(room, start, end))
    if busy:
        filtered: list[Candidate] = []
        for cand in out:
            intervals = busy.get(cand.room.object_id)
            if intervals and any(cand.overlaps(s, e) for s, e in intervals):
                continue
            filtered.append(cand)
        out = filtered
    return out


def drop_conflicting(candidates: list[Candidate], booked: Candidate) -> list[Candidate]:
    """After a success, remove candidates in the same room or overlapping times."""
    return [
        c
        for c in candidates
        if c.room.object_id != booked.room.object_id and not c.overlaps(booked.start_min, booked.end_min)
    ]


def target_date(release: ReleaseConfig, now: datetime) -> date:
    """The day that opens at tonight's release (today + days_ahead)."""
    return (now + timedelta(days=release.days_ahead)).date()


def weekday_enabled(strategy: StrategyConfig, day: date) -> bool:
    return WEEKDAYS[day.weekday()] in strategy.weekdays


def iso_week(day: date) -> tuple[int, int]:
    iso = day.isocalendar()
    return iso[0], iso[1]


def _as_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if not value:
        return None
    text = str(value)
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text[: len(fmt) + 2], fmt).date()
        except ValueError:
            continue
    return None


def reservations_in_week(day: date, reservations: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    week = iso_week(day)
    out = []
    for res in reservations:
        res_day = _as_date(res.get("date"))
        if res_day is not None and iso_week(res_day) == week:
            out.append(res)
    return out


def quota_remaining(strategy: StrategyConfig, day: date, reservations: Iterable[dict[str, Any]]) -> int:
    used = len(reservations_in_week(day, reservations))
    return max(0, strategy.weekly_quota - used)
