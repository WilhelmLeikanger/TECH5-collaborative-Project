"""Parse TimeEdit's week-grid HTML fragment into per-room busy intervals.

The fragment returned by ``ri.html?part=t&partajax=t&objects=...`` lays out
one ``div.weekDay[data-day=YYYYMMDD]`` per day.  Inside, ``div.weekDiv``
holds a header line ``.weekHourLine`` with one ``.hour[data-object]`` per room
(the row order) and one ``div.bookingDiv[title="YYYY-MM-DD HH:MM - HH:MM, ..."]``
per reservation, positioned with inline ``top``/``height`` styles.  The row a
booking belongs to is ``floor(top / rowHeight)``.

Only the standard library is used, and everything is best-effort: when the
markup does not match, :func:`parse_grid` returns an empty result and the bot
simply tries its candidates without pre-filtering.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Iterator

from .config import parse_hhmm

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
_TITLE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})")
_TOP_RE = re.compile(r"\btop:\s*(-?\d+(?:\.\d+)?)px", re.I)
_HEIGHT_RE = re.compile(r"\bheight:\s*(-?\d+(?:\.\d+)?)px", re.I)


class Node:
    __slots__ = ("tag", "attrs", "children", "parent", "text")

    def __init__(self, tag: str, attrs: dict[str, str], parent: "Node | None") -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[Node] = []
        self.parent = parent
        self.text: list[str] = []

    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())

    def iter(self) -> Iterator["Node"]:
        yield self
        for child in self.children:
            yield from child.iter()

    def find_all(self, tag: str | None = None, cls: str | None = None, attr: str | None = None) -> list["Node"]:
        out = []
        for node in self.iter():
            if node is self:
                continue
            if tag and node.tag != tag:
                continue
            if cls and cls not in node.classes():
                continue
            if attr and attr not in node.attrs:
                continue
            out.append(node)
        return out

    def find(self, tag: str | None = None, cls: str | None = None, attr: str | None = None) -> "Node | None":
        found = self.find_all(tag, cls, attr)
        return found[0] if found else None

    def inner_text(self) -> str:
        parts: list[str] = []
        for node in self.iter():
            parts.extend(node.text)
        return re.sub(r"\s+", " ", " ".join(parts)).strip()


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("document", {}, None)
        self.current = self.root

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, {k: (v or "") for k, v in attrs}, self.current)
        self.current.children.append(node)
        if tag not in _VOID:
            self.current = node

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.current.children.append(Node(tag, {k: (v or "") for k, v in attrs}, self.current))

    def handle_endtag(self, tag: str) -> None:
        node: Node | None = self.current
        while node is not None and node is not self.root:
            if node.tag == tag:
                self.current = node.parent or self.root
                return
            node = node.parent

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.current.text.append(data)


def parse_html(html: str) -> Node:
    builder = _Builder()
    builder.feed(html)
    builder.close()
    return builder.root


@dataclass
class Booking:
    day: str  # YYYY-MM-DD
    room: str  # object id "<id>.<type>"
    start_min: int
    end_min: int
    label: str = ""


@dataclass
class Grid:
    days: list[str] = field(default_factory=list)
    rooms: list[str] = field(default_factory=list)
    bookings: list[Booking] = field(default_factory=list)
    rules: str = ""

    def busy(self, day: str) -> dict[str, list[tuple[int, int]]]:
        out: dict[str, list[tuple[int, int]]] = {room: [] for room in self.rooms}
        for booking in self.bookings:
            if booking.day == day:
                out.setdefault(booking.room, []).append((booking.start_min, booking.end_min))
        return out


def _compact_to_iso(value: str) -> str | None:
    if re.fullmatch(r"\d{8}", value):
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    return None


def parse_grid(html: str) -> Grid:
    """Best-effort parse of the week grid; see module docstring."""
    root = parse_html(html)
    grid = Grid()
    rules = root.find(cls="textHTML")
    if rules is not None:
        grid.rules = rules.inner_text()
    for week_day in root.find_all(cls="weekDay", attr="data-day"):
        day = _compact_to_iso(week_day.attrs.get("data-day", ""))
        if not day:
            continue
        grid.days.append(day)
        week_divs = [n for n in week_day.find_all(cls="weekDiv") if "data-hourheight" in n.attrs or "data-object" in n.attrs]
        if not week_divs:
            week_divs = week_day.find_all(cls="weekDiv")
        for week_div in week_divs:
            hour_line = week_div.find(cls="weekHourLine")
            rows: list[str] = []
            if hour_line is not None:
                rows = [n.attrs["data-object"] for n in hour_line.find_all(cls="hour", attr="data-object") if n.attrs.get("data-object")]
            if not rows and week_div.attrs.get("data-object"):
                rows = [week_div.attrs["data-object"]]
            if not rows:
                continue
            for room in rows:
                if room not in grid.rooms:
                    grid.rooms.append(room)
            style = week_div.attrs.get("style", "")
            height_match = _HEIGHT_RE.search(style)
            total_height = float(height_match.group(1)) if height_match else 40.0 * len(rows)
            row_height = total_height / max(1, len(rows))
            for node in week_div.find_all(cls="bookingDiv", attr="title"):
                title = node.attrs.get("title", "")
                match = _TITLE_RE.match(title)
                if not match:
                    continue
                booking_day = match.group(1)
                start_min, end_min = parse_hhmm(match.group(2)), parse_hhmm(match.group(3))
                if end_min <= start_min:  # ends at midnight
                    end_min = 24 * 60
                top_match = _TOP_RE.search(node.attrs.get("style", ""))
                top = float(top_match.group(1)) if top_match else 0.0
                row = min(max(0, int(top // row_height)), len(rows) - 1) if len(rows) > 1 else 0
                label = title[match.end():].strip(" ,")
                grid.bookings.append(Booking(booking_day, rows[row], start_min, end_min, label))
    return grid
