"""A small fake TimeEdit server implementing the reservation protocol.

Used by the test-suite and ``te_bot selftest`` so the whole pipeline (login
check, discovery, planning, clock sync, release-time burst, fallbacks) can be
exercised locally.  The responses mimic the real server closely enough for
the bot's parsers and classifiers: CSRF tokens, ``302`` with ``id`` on
success, ``409`` with explanatory text on failure, ``412`` when not logged
in, ``objects.json`` searches, the week-grid fragment and ``my.json``.
"""

from __future__ import annotations

import json
import random
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlparse

DEFAULT_ROOMS = (
    [f"Grupperom 10{i}" for i in range(1, 8)]
    + [f"Grupperom U0{i}" for i in range(1, 7)]
    + [f"Grupperom U{i}" for i in range(20, 33)]
)


@dataclass
class Reservation:
    id: int
    room: str  # object id "<id>.<type>"
    day: date
    start_min: int
    end_min: int
    user: str = "me"
    label: str = "Group work"


@dataclass
class MockTimeEdit:
    org: str = "nhh"
    web_type: str = "schedule"
    reserve_page: str = "ri1Q56.html"
    sid: int = 12
    room_type: int = 186
    person_type: int = 190
    days_ahead: int = 14
    max_hours: float = 4.0
    weekly_quota: int = 4
    #: Epoch seconds at which day ``today + days_ahead`` becomes bookable.
    release_at: float | None = None
    today: date | None = None
    cookie_name: str = "TEnhhweb"
    cookie_value: str = "ok"
    require_csrf: bool = True
    clock_offset_s: float = 0.0
    #: Rooms blocked for everyone (like NHH's U21–U27 "master" rooms in the screenshot).
    blocked_rooms: tuple[str, ...] = ()
    rooms: list[tuple[str, str]] = field(default_factory=list)  # (object id, name)
    reservations: list[Reservation] = field(default_factory=list)
    tokens: set[str] = field(default_factory=set)
    counters: Counter = field(default_factory=Counter)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    lock: threading.RLock = field(default_factory=threading.RLock)
    _next_id: int = 1000

    def __post_init__(self) -> None:
        if not self.rooms:
            self.rooms = [(f"{1000 + i}.{self.room_type}", name) for i, name in enumerate(DEFAULT_ROOMS)]
        if self.today is None:
            self.today = date.today()

    # -- helpers -----------------------------------------------------------
    @property
    def base_path(self) -> str:
        return f"/{self.org}/web/{self.web_type}"

    def room_name(self, obj: str) -> str:
        for object_id, name in self.rooms:
            if object_id == obj:
                return name
        return obj

    def now(self) -> float:
        return time.time() + self.clock_offset_s

    def is_open(self, day: date) -> bool:
        assert self.today is not None
        horizon = self.today + timedelta(days=self.days_ahead)
        if day < horizon:
            return True
        if day == horizon:
            return self.release_at is None or self.now() >= self.release_at
        return False

    def busy(self, obj: str, day: date, start_min: int, end_min: int) -> bool:
        for res in self.reservations:
            if res.room == obj and res.day == day and res.start_min < end_min and start_min < res.end_min:
                return True
        return False

    def add_reservation(self, obj: str, day: date, start_min: int, end_min: int, user: str = "other", label: str = "") -> Reservation:
        with self.lock:
            res = Reservation(self._next_id, obj, day, start_min, end_min, user, label or ("Group work" if user == "me" else "Auditorium C"))
            self._next_id += 1
            self.reservations.append(res)
            return res

    # -- protocol ----------------------------------------------------------
    def reserve(self, form: list[tuple[str, str]]) -> tuple[int, dict[str, str], str]:
        fields = dict(form)
        objects = [v for k, v in form if k == "o"]
        rooms = [o for o in objects if o.endswith(f".{self.room_type}")]
        self.counters["reserve"] += 1
        self.attempts.append({"t": self.now(), "fields": fields, "objects": objects})
        if self.require_csrf and fields.get("CSTT") not in self.tokens:
            return 409, {}, "The request could not be processed. Invalid CSTT token."
        if not rooms:
            return 409, {}, "The reservation could not be made. No room selected."
        obj = rooms[0]
        try:
            day = datetime.strptime(fields["dates"], "%Y%m%d").date()
            start = fields.get("starttime") or fields.get("startTime", "")
            end = fields.get("endtime") or fields.get("endTime", "")
            start_min = int(start[:2]) * 60 + int(start[-2:])
            end_min = int(end[:2]) * 60 + int(end[-2:])
        except (KeyError, ValueError):
            return 409, {}, "The reservation could not be made. Invalid date or time."
        if not self.is_open(day):
            return 409, {}, "The reservation could not be made. The reservation is outside the date limits."
        if (end_min - start_min) > self.max_hours * 60:
            return 409, {}, "The reservation could not be made. The user's maximum booking length has been exceeded."
        if self.room_name(obj) in self.blocked_rooms or self.busy(obj, day, start_min, end_min):
            return 409, {}, f"The reservation could not be made. The time is occupied because the object {self.room_name(obj)} is already reserved."
        week = day.isocalendar()[:2]
        mine = [r for r in self.reservations if r.user == "me" and r.day.isocalendar()[:2] == week]
        if len(mine) >= self.weekly_quota:
            return 409, {}, "The reservation could not be made. The maximum number of reservations per week has been exceeded."
        res = self.add_reservation(obj, day, start_min, end_min, user="me")
        location = f"{self.base_path}/{self.reserve_page}?h=t&sid={self.sid}&id={res.id}&step=3&newres=t"
        return 302, {"Location": location}, ""

    def objects_json(self, query: dict[str, list[str]]) -> str:
        types = query.get("types", [None])[0]
        search = (query.get("search_text", [""])[0] or "").lower()
        rows = []
        day = None
        start_min = end_min = None
        if query.get("dates"):
            try:
                day = datetime.strptime(query["dates"][0][:8], "%Y%m%d").date()
                if query.get("starttime") and query.get("endtime"):
                    s, e = query["starttime"][0], query["endtime"][0]
                    start_min = int(s.split(":")[0]) * 60 + int(s.split(":")[1] if ":" in s else 0)
                    end_min = int(e.split(":")[0]) * 60 + int(e.split(":")[1] if ":" in e else 0)
            except ValueError:
                day = None
        for object_id, name in self.rooms:
            if types and not object_id.endswith(f".{types}"):
                continue
            if search and search not in name.lower():
                continue
            if day is not None and start_min is not None and end_min is not None:
                if not self.is_open(day) or self.busy(object_id, day, start_min, end_min) or name in self.blocked_rooms:
                    continue
            obj_id, obj_type = object_id.split(".")
            rows.append({"id": obj_id, "idAndType": object_id, "type": obj_type, "fields": {"Name": name, "Type": "Grupperom"}})
        if not rows:
            return json.dumps("No search results")
        return json.dumps({"objects": rows, "hasMore": False})

    def grid_html(self, objects: list[str], day_from: date, day_to: date) -> str:
        rows = [o for o in objects if o]
        row_height = 40
        parts = ['<div class="textHTML">Max 4 hours per reservation.<br/>Max 4 reservations per week.</div>']
        day = day_from
        while day <= day_to:
            parts.append(f'<div class="weekDay" data-day="{day:%Y%m%d}">')
            parts.append(f'<div class="weekDiv" data-hourHeight="{row_height}" style="height: {row_height * len(rows)}px">')
            parts.append('<div class="weekHourLine">' + "".join(f'<div class="hour" data-object="{o}"></div>' for o in rows) + "</div>")
            for res in self.reservations:
                if res.day != day or res.room not in rows:
                    continue
                top = rows.index(res.room) * row_height
                title = f"{day:%Y-%m-%d} {res.start_min // 60:02d}:{res.start_min % 60:02d} - {res.end_min // 60:02d}:{res.end_min % 60:02d}, {res.label}, ID {res.id}"
                parts.append(f'<div class="bookingDiv" title="{title}" style="top: {top}px; height: {row_height}px; left: 10px"></div>')
            for name in self.blocked_rooms:
                obj = next((o for o, n in self.rooms if n == name), None)
                if obj in rows:
                    top = rows.index(obj) * row_height
                    parts.append(f'<div class="bookingDiv" title="{day:%Y-%m-%d} 00:00 - 00:00, Blocked, ID 1" style="top: {top}px; height: {row_height}px"></div>')
            parts.append("</div></div>")
            day += timedelta(days=1)
        return "\n".join(parts)

    def form_html(self, obj: str) -> str:
        return (
            '<form id="resform">'
            f'<input type="hidden" name="o" value="{obj}"/>'
            '<input type="hidden" name="o" value="777.190"/>'
            '<input type="text" name="fe2" value=""/>'
            '<select name="fe9"><option value="">-</option><option value="3" selected>3</option></select>'
            '<textarea name="fe3"></textarea>'
            "</form>"
        )

    def my_json(self) -> str:
        mine = [r for r in self.reservations if r.user == "me"]
        return json.dumps(
            {
                "reservationcount": len(mine),
                "reservations": [
                    {
                        "id": r.id,
                        "startdate": r.day.isoformat(),
                        "starttime": f"{r.start_min // 60:02d}:{r.start_min % 60:02d}",
                        "enddate": r.day.isoformat(),
                        "endtime": f"{r.end_min // 60:02d}:{r.end_min % 60:02d}",
                        "columns": [self.room_name(r.room), r.label],
                    }
                    for r in mine
                ],
            }
        )

    def page_html(self, logged_in: bool) -> str:
        rows = "".join(
            f'<div class="roomRow" data-object="{o}" data-name="{n}">{n}</div>' for o, n in self.rooms
        )
        sso = f"{self.base_path.rsplit('/', 1)[0]}/timeedit/sso/feide?back={self.base_path}/{self.reserve_page}"
        header = (
            f'<div id="logincontrol"><a href="{sso}">Log in</a></div>'
            if not logged_in
            else '<div id="usercontrol">Logged in as Student <a href="/logout">Log out</a></div>'
        )
        my = '<h2>My reservations</h2>' if logged_in else ""
        return (
            f"<html><head><title>TimeEdit - NHH</title></head><body>{header}"
            f'<script>var sid={self.sid}; var url="ri.html?h=t&sid={self.sid}";</script>'
            f'<div class="grid">{rows}</div>{my}</body></html>'
        )


class _Handler(BaseHTTPRequestHandler):
    server_version = "MockTimeEdit/1.0"
    mock: MockTimeEdit  # set on the server class

    def log_message(self, fmt: str, *args: Any) -> None:  # silence
        pass

    def date_time_string(self, timestamp: float | None = None) -> str:
        return super().date_time_string(self.mock.now() if timestamp is None else timestamp)

    # -- utilities ---------------------------------------------------------
    def _logged_in(self) -> bool:
        cookie = self.headers.get("Cookie", "")
        return f"{self.mock.cookie_name}={self.mock.cookie_value}" in cookie

    def _send(self, status: int, body: str, content_type: str = "text/plain; charset=UTF-8", headers: dict[str, str] | None = None) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _not_logged_in(self) -> None:
        self._send(412, "<html><body><form><input name='username'/><input name='password' type='password'/></form>Not logged in</body></html>", "text/html; charset=UTF-8")

    def _route(self) -> tuple[str, dict[str, list[str]]]:
        parsed = urlparse(self.path)
        path = parsed.path
        base = self.mock.base_path
        if path.startswith(base + "/"):
            path = path[len(base) + 1 :]
        return path, parse_qs(parsed.query, keep_blank_values=True)

    # -- verbs -------------------------------------------------------------
    SSO_PATH = "/timeedit/sso/feide"

    def _sso_form(self, error: str = "") -> str:
        return (
            "<html><head><title>Feide</title></head><body>"
            f"<p style='color:red'>{error}</p>"
            f"<form method='post' action='{self.mock.base_path.rsplit('/', 1)[0]}{self.SSO_PATH}'>"
            "<label>Username <input id='username' name='username' type='text'/></label>"
            "<label>Password <input id='password' name='password' type='password'/></label>"
            "<button type='submit'>Log in</button></form></body></html>"
        )

    def do_GET(self) -> None:  # noqa: N802
        path, query = self._route()
        mock = self.mock
        mock.counters[f"GET {path}"] += 1
        if path.endswith(self.SSO_PATH):
            return self._send(200, self._sso_form(), "text/html; charset=UTF-8")
        if path == mock.reserve_page or path == "":
            self._send(200, mock.page_html(self._logged_in()), "text/html; charset=UTF-8")
        elif path == "objects.json":
            if not self._logged_in():
                return self._not_logged_in()
            self._send(200, mock.objects_json(query), "application/json; charset=UTF-8")
        elif path == "ri.html":
            if not self._logged_in():
                return self._not_logged_in()
            if query.get("step", [""])[0] == "2":
                self._send(200, mock.form_html(query.get("o", [""])[0]), "text/html; charset=UTF-8")
                return
            objects = query.get("objects", [""])[0].split(",")
            p = query.get("p", [""])[0]
            today = mock.today or date.today()
            if "-" in p and p.replace("-", "").isdigit():
                day_from = datetime.strptime(p.split("-")[0], "%Y%m%d").date()
                day_to = datetime.strptime(p.split("-")[1], "%Y%m%d").date()
            elif ".w" in p:
                weeks = int(p.split(".")[0])
                monday = today - timedelta(days=today.weekday()) + timedelta(weeks=weeks)
                day_from, day_to = monday, monday + timedelta(days=6)
            else:
                day_from = today - timedelta(days=today.weekday())
                day_to = day_from + timedelta(days=6)
            self._send(200, mock.grid_html(objects, day_from, day_to), "text/html; charset=UTF-8")
        elif path == "my.json":
            if not self._logged_in():
                return self._not_logged_in()
            self._send(200, mock.my_json(), "application/json; charset=UTF-8")
        else:
            self._send(404, "not found")

    def do_POST(self) -> None:  # noqa: N802
        path, _query = self._route()
        mock = self.mock
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        form = parse_qsl(raw, keep_blank_values=True)
        fields = dict(form)
        mock.counters[f"POST {path}"] += 1
        if path.endswith(self.SSO_PATH):  # fake Feide: any user with password "secret"
            if fields.get("username") and fields.get("password") == "secret":
                return self._send(
                    302,
                    "",
                    headers={
                        "Set-Cookie": f"{mock.cookie_name}={mock.cookie_value}; Path=/; HttpOnly",
                        "Location": f"{mock.base_path}/{mock.reserve_page}",
                    },
                )
            return self._send(200, self._sso_form("Wrong username or password"), "text/html; charset=UTF-8")
        if path != mock.reserve_page:
            return self._send(404, "not found")
        if not self._logged_in():
            return self._not_logged_in()
        if fields.get("CSTTG") == "gen":
            token = str(random.randint(10**8, 10**9))
            with mock.lock:
                mock.tokens.add(token)
            return self._send(200, token)
        if fields.get("kind") == "reserve":
            with mock.lock:
                status, headers, body = mock.reserve(form)
            return self._send(status, body, headers=headers)
        self._send(400, "unsupported")

    def do_DELETE(self) -> None:  # noqa: N802
        path, query = self._route()
        mock = self.mock
        if path != "my.html":
            return self._send(404, "not found")
        if not self._logged_in():
            return self._not_logged_in()
        res_id = query.get("id", [""])[0]
        with mock.lock:
            before = len(mock.reservations)
            mock.reservations = [r for r in mock.reservations if str(r.id) != res_id]
        if len(mock.reservations) < before:
            self._send(200, "Cancellation completed")
        else:
            self._send(409, "Could not cancel: reservation not found")


class _Server(ThreadingHTTPServer):
    """ThreadingHTTPServer without the reverse-DNS lookup in ``server_bind``."""

    daemon_threads = True

    def server_bind(self) -> None:
        import socketserver

        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


def start_server(mock: MockTimeEdit, host: str = "127.0.0.1", port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    """Start the mock in a daemon thread; returns ``(server, base_url)``."""
    handler = type("MockHandler", (_Handler,), {"mock": mock})
    server = _Server((host, port), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="mock-timeedit", daemon=True)
    thread.start()
    base_url = f"http://{host}:{server.server_address[1]}{mock.base_path}"
    return server, base_url


def serve(port: int = 8765, release_in_s: float = 60.0) -> None:  # pragma: no cover - manual use
    mock = MockTimeEdit(release_at=time.time() + release_in_s)
    server, base_url = start_server(mock, port=port)
    print(f"Mock TimeEdit at {base_url}/{mock.reserve_page}")
    print(f"Day {mock.today + timedelta(days=mock.days_ahead)} opens in {release_in_s:.0f}s; session cookie {mock.cookie_name}={mock.cookie_value}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.shutdown()
