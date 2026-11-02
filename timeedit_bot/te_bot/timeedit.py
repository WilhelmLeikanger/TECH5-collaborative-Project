"""HTTP client for TimeEdit's web reservation interface ("TE Reserve").

The protocol below is the one the browser uses on ``cloud.timeedit.net`` and
is shared by every institution on that host (it was cross-checked against
several independent implementations for Chalmers, LiU, HiG/NTNU):

* ``POST <reserve page>`` with ``CSTTG=gen``            → CSRF token (plain integer)
* ``POST <reserve page>`` with ``kind=reserve`` + fields → ``302`` with ``Location: ...?id=<reservation id>``
  on success, ``409`` with a human readable message otherwise
* ``GET  objects.json``                                 → room search (optionally filtered to free rooms)
* ``GET  ri.html?part=t&partajax=t&...``                → week grid (HTML fragment)
* ``GET  my.json`` / ``DELETE my.html?id=``             → list / cancel own reservations

Authentication is a session cookie (``TE<org>web``) obtained by logging in
through the institution's SSO in a real browser (see :mod:`te_bot.auth`).
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlparse

import requests

from .config import TimeEditConfig

log = logging.getLogger("te_bot.timeedit")


class Outcome(str, Enum):
    SUCCESS = "success"
    NOT_OPEN = "not_open"  # date is still outside the booking window → retry
    OCCUPIED = "occupied"  # someone else has (part of) the slot → next candidate
    LENGTH = "length"  # booking too long for the rules → try shorter
    QUOTA = "quota"  # too many bookings for this user → stop
    NOT_LOGGED_IN = "not_logged_in"
    CSRF = "csrf"  # token rejected → refresh and retry
    UNKNOWN = "unknown"


# Keyword tables (lower-case).  Order matters: more specific categories first.
_NOT_OPEN = (
    "outside the date",
    "outside date",
    "date limit",
    "date limits",
    "datumgräns",
    "datogrens",
    "utenfor dato",
    "innenfor dato",
    "ikke innenfor",
    "not within the date",
    "not within date",
    "too far",
    "for langt",
    "frem i tid",
    "fram i tid",
    "framover i tid",
    "cannot be made yet",
    "ennå ikke",
    "not yet",
    "days ahead",
    "dager frem",
    "dagar fram",
)
_LENGTH = (
    "booking length",
    "reservation length",
    "bokningslängd",
    "bookinglengde",
    "reservasjonslengde",
    "maximum length",
    "maksimal lengde",
    "maximal längd",
    "too long",
    "for lang",
    "för lång",
    "duration",
    "varighet",
    "varaktighet",
    "maximum time",
    "maksimal tid",
    "max time",
    "hours per",
    "timer per",
    "timmar per",
)
_QUOTA_STRONG = (
    "maximum number",
    "maksimalt antall",
    "maksimum antall",
    "maximalt antal",
    "too many",
    "for mange",
    "för många",
    "quota",
    "kvote",
    "kvot",
    "per week",
    "per uke",
    "per vecka",
    "no more",
    "ikke flere",
    "inga fler",
    "reservations this week",
    "reservasjoner denne uken",
)
_QUOTA = ("exceeded", "overskredet", "överskridits", "limit", "grense", "gräns")
_OCCUPIED = (
    "occupied",
    "opptatt",
    "upptagen",
    "already",
    "allerede",
    "redan",
    "collision",
    "kollisjon",
    "kollision",
    "not available",
    "ikke tilgjengelig",
    "ikke ledig",
    "inte tillgänglig",
    "busy",
    "conflict",
    "konflikt",
    "overlap",
    "overlapp",
    "taken",
    "reserved by",
    "booked by",
)
_NOT_LOGGED_IN = (
    "not logged in",
    "ikke logget inn",
    "ikke innlogget",
    "inte inloggad",
    "please log in",
    "logg inn",
    "log in",
    "login",
    "logga in",
)
_CSRF = ("cstt", "csrf", "token", "invalid request", "ugyldig forespørsel")


def _contains(text: str, needles: Iterable[str]) -> bool:
    return any(needle in text for needle in needles)


def classify(status: int, text: str, location: str | None = None) -> tuple[Outcome, str]:
    """Classify a reservation response into an :class:`Outcome`.

    Returns the outcome and a short human-readable message.
    """
    body = (text or "").strip()
    lowered = body.lower()
    if status in (301, 302, 303, 307, 308):
        if location and ("id=" in location or "newres" in location):
            return Outcome.SUCCESS, f"redirect → {location}"
        if location and _contains(location.lower(), ("login", "sso", "logg")):
            return Outcome.NOT_LOGGED_IN, f"redirected to login: {location}"
        return Outcome.UNKNOWN, f"redirect without id → {location}"
    if status == 200 and lowered.startswith("http") and ("id=" in lowered) and (
        "newres" in lowered or "step=3" in lowered
    ):
        return Outcome.SUCCESS, f"ok → {body[:200]}"
    if status in (401, 403, 412) or (status == 200 and "<form" in lowered and _contains(lowered, ("password", "passord", "lösenord"))):
        return Outcome.NOT_LOGGED_IN, f"HTTP {status}: {_squash(body)}"
    if _contains(lowered, _NOT_LOGGED_IN) and status != 409 and len(body) < 400:
        return Outcome.NOT_LOGGED_IN, f"HTTP {status}: {_squash(body)}"
    if _contains(lowered, _NOT_OPEN):
        return Outcome.NOT_OPEN, _squash(body)
    if _contains(lowered, _LENGTH):
        return Outcome.LENGTH, _squash(body)
    if _contains(lowered, _QUOTA_STRONG):
        return Outcome.QUOTA, _squash(body)
    if _contains(lowered, _OCCUPIED):
        return Outcome.OCCUPIED, _squash(body)
    if _contains(lowered, _CSRF) and len(body) < 300:
        return Outcome.CSRF, f"HTTP {status}: {_squash(body)}"
    if _contains(lowered, _QUOTA):
        return Outcome.QUOTA, _squash(body)
    if status == 409:
        return Outcome.UNKNOWN, f"HTTP 409: {_squash(body)}"
    return Outcome.UNKNOWN, f"HTTP {status}: {_squash(body)}"


def _squash(text: str, limit: int = 300) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


@dataclass
class ReserveResult:
    outcome: Outcome
    status: int
    message: str
    reservation_id: str | None = None
    elapsed_ms: float = 0.0
    sent_at: float = 0.0
    body: str = ""


@dataclass
class TEObject:
    """A TimeEdit object (room, person, course ...)."""

    id: str  # e.g. "12345"
    type: str  # e.g. "186"
    name: str
    fields: dict[str, Any] = field(default_factory=dict)

    @property
    def object_id(self) -> str:
        """``<id>.<type>`` as used in the ``o`` / ``objects`` parameters."""
        return f"{self.id}.{self.type}"


class NotLoggedIn(RuntimeError):
    """The session cookie is missing or expired."""


class TimeEditClient:
    """Thin, fast client around :mod:`requests` for one TimeEdit instance."""

    def __init__(
        self,
        cfg: TimeEditConfig,
        cookies: Any = None,
        template: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> None:
        self.cfg = cfg
        self.template = template
        #: Additional ``o`` objects (e.g. the booking person) sent when no template is used.
        self.extra_objects: list[str] = []
        self.timeout = timeout or cfg.request_timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": cfg.user_agent,
                "Accept": "*/*",
                "Accept-Language": "en-GB,en;q=0.9,nb;q=0.8",
                "Origin": cfg.origin,
                "Referer": cfg.reserve_url,
                "X-Requested-With": "XMLHttpRequest",
            }
        )
        self._last_request_at = 0.0
        if cookies:
            self.set_cookies(cookies)

    # -- cookies ---------------------------------------------------------
    def set_cookies(self, cookies: Any) -> None:
        """Accept a Playwright storage-state dict, a cookie list, or a dict."""
        if isinstance(cookies, dict) and "cookies" in cookies:
            cookies = cookies["cookies"]
        if isinstance(cookies, dict):
            for name, value in cookies.items():
                self.session.cookies.set(name, value, domain=self._host)
            return
        for cookie in cookies:
            domain = cookie.get("domain") or self._host
            self.session.cookies.set(
                cookie["name"], cookie["value"], domain=domain.lstrip("."), path=cookie.get("path", "/")
            )

    @property
    def _host(self) -> str:
        return urlparse(self.cfg.base_url).hostname or ""

    def cookie_names(self) -> list[str]:
        return [c.name for c in self.session.cookies]

    def has_te_cookie(self) -> bool:
        return any(name.lower().startswith("te") and "web" in name.lower() for name in self.cookie_names())

    # -- low level -------------------------------------------------------
    def _url(self, path: str) -> str:
        if path.startswith("http"):
            return path
        return f"{self.cfg.base_url.rstrip('/')}/{path.lstrip('/')}"

    def _throttle(self, min_gap_ms: float = 0.0) -> None:
        if min_gap_ms <= 0:
            return
        wait = self._last_request_at + min_gap_ms / 1000.0 - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("allow_redirects", False)
        self._last_request_at = time.monotonic()
        response = self.session.request(method, self._url(path), **kwargs)
        log.debug("%s %s → %s (%d bytes)", method, response.url, response.status_code, len(response.content))
        return response

    def ping(self) -> tuple[float, float, datetime | None]:
        """Cheap request used for clock sync.

        Returns ``(sent_monotonic, received_monotonic, server_date)``.
        """
        sent = time.monotonic()
        response = self.session.get(
            self._url(self.cfg.reserve_page),
            params={"nocache": random.randint(1, 10**6)},
            timeout=self.timeout,
            allow_redirects=False,
            stream=True,
        )
        received = time.monotonic()
        server_date = None
        raw = response.headers.get("Date")
        if raw:
            try:
                server_date = parsedate_to_datetime(raw)
                if server_date.tzinfo is None:
                    server_date = server_date.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                server_date = None
        response.close()
        return sent, received, server_date

    # -- session ---------------------------------------------------------
    def my_reservations_raw(self) -> tuple[int, Any]:
        """Return ``(status, json_or_text)`` for ``my.json``."""
        response = self.request("GET", "my.json", headers={"Accept": "application/json, text/javascript, */*"})
        text = response.text
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, text

    def is_logged_in(self) -> bool:
        status, data = self.my_reservations_raw()
        if status == 200 and isinstance(data, dict) and (
            "reservations" in data or "reservationcount" in data
        ):
            return True
        if status in (401, 403, 412):
            return False
        # Fall back to inspecting the reserve page itself.
        page = self.request("GET", self.cfg.reserve_page, headers={"Accept": "text/html"})
        return looks_logged_in(page.text)

    def my_reservations(self) -> list[dict[str, Any]]:
        status, data = self.my_reservations_raw()
        if status in (401, 403, 412):
            raise NotLoggedIn(f"my.json → HTTP {status}")
        if not isinstance(data, dict):
            raise NotLoggedIn("my.json did not return JSON (session expired?)")
        reservations = data.get("reservations") or []
        return [normalise_reservation(r) for r in reservations]

    # -- reservations ----------------------------------------------------
    def get_csrf(self) -> str:
        """Fetch a fresh CSRF token (``CSTTG=gen`` → plain integer)."""
        response = self.request(
            "POST",
            self.cfg.reserve_page,
            data={"CSTTG": "gen"},
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        )
        token = response.text.strip()
        if response.status_code in (401, 403, 412):
            raise NotLoggedIn(f"CSTTG → HTTP {response.status_code}")
        if response.status_code != 200 or not re.fullmatch(r"-?\d+", token):
            raise RuntimeError(f"Unexpected CSRF response {response.status_code}: {_squash(token)}")
        return token

    def build_reserve_body(
        self,
        room_object: str,
        day: date,
        start: str,
        end: str,
        csrf: str | None,
        fields: dict[str, str] | None = None,
    ) -> list[tuple[str, str]]:
        """Build the form body, from the learned template when available."""
        dates = day.strftime("%Y%m%d")
        if self.template and self.template.get("fields"):
            room_type = room_object.split(".")[-1]
            learned_room = str(self.template.get("room_object") or "")
            body: list[tuple[str, str]] = []
            seen: set[str] = set()
            for key, value in self.template["fields"]:
                key = str(key)
                value = "" if value is None else str(value)
                if key == "o":
                    if value == learned_room or (not learned_room and value.endswith("." + room_type)):
                        value = room_object
                elif key == "dates":
                    value = dates
                elif key in ("starttime", "startTime"):
                    value = start
                elif key in ("endtime", "endTime"):
                    value = end
                elif key == "CSTT":
                    value = csrf or value
                elif key == "nocache":
                    value = str(random.randint(1, 20))
                elif fields and key in fields:
                    value = fields[key]
                seen.add(key)
                body.append((key, value))
            if csrf and "CSTT" not in seen:
                body.append(("CSTT", csrf))
            for key, value in (fields or {}).items():
                if key not in seen:
                    body.append((key, value))
            return body
        body = [
            ("kind", "reserve"),
            ("nocache", str(random.randint(1, 20))),
        ]
        if self.cfg.locale:
            body.append(("l", self.cfg.locale))
        body.append(("o", room_object))
        for obj in self.extra_objects:
            if obj != room_object:
                body.append(("o", obj))
        body += [
            ("aos", ""),
            ("dates", dates),
            ("starttime", start),
            ("endtime", end),
            ("url", self.cfg.reserve_url),
        ]
        for key, value in (fields or {}).items():
            body.append((key, value))
        if csrf:
            body.append(("CSTT", csrf))
        return body

    def reserve(
        self,
        room_object: str,
        day: date,
        start: str,
        end: str,
        csrf: str | None,
        fields: dict[str, str] | None = None,
        min_gap_ms: float = 0.0,
    ) -> ReserveResult:
        """Attempt one reservation.  Never raises for protocol-level failures."""
        body = self.build_reserve_body(room_object, day, start, end, csrf, fields)
        url = self._url(self.cfg.reserve_page)
        if self.template and self.template.get("url"):
            url = self.template["url"]
        self._throttle(min_gap_ms)
        started = time.monotonic()
        sent_at = time.time()
        try:
            response = self.request(
                "POST",
                url,
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
            )
        except requests.RequestException as exc:
            return ReserveResult(Outcome.UNKNOWN, 0, f"network error: {exc}", elapsed_ms=(time.monotonic() - started) * 1000, sent_at=sent_at)
        elapsed = (time.monotonic() - started) * 1000
        location = response.headers.get("Location")
        outcome, message = classify(response.status_code, response.text, location)
        reservation_id = None
        if outcome is Outcome.SUCCESS:
            reservation_id = extract_reservation_id(location or response.text)
        return ReserveResult(outcome, response.status_code, message, reservation_id, elapsed, sent_at, response.text[:2000])

    def cancel(self, reservation_id: str) -> tuple[bool, str]:
        params = {"id": reservation_id, "sid": self.cfg.sid}
        if self.cfg.locale:
            params["l"] = self.cfg.locale
        response = self.request("DELETE", "my.html", params=params, headers={"Accept": "text/plain, */*; q=0.01"})
        text = _squash(response.text)
        ok = response.status_code == 200 and not _contains(text.lower(), ("error", "feil", "kunne ikke", "could not"))
        return ok, f"HTTP {response.status_code}: {text}"

    # -- discovery helpers ----------------------------------------------
    def fetch_reserve_page(self) -> str:
        response = self.request("GET", self.cfg.reserve_page, headers={"Accept": "text/html"}, allow_redirects=True)
        return response.text

    def objects(
        self,
        types: int | str | None = None,
        search_text: str = "",
        day: date | None = None,
        start: str | None = None,
        end: str | None = None,
        max_results: int = 200,
        extra: dict[str, Any] | None = None,
    ) -> list[TEObject]:
        """Search objects (rooms).  With day/start/end only *free* rooms are returned."""
        params: dict[str, Any] = {
            "max": max_results,
            "fr": "f",
            "part": "t",
            "partajax": "t",
            "im": "f",
            "step": 1,
            "sid": self.cfg.sid,
        }
        if self.cfg.locale:
            params["l"] = self.cfg.locale
        if types is not None:
            params["types"] = types
            params["subtypes"] = types
        if search_text:
            params["search_text"] = search_text
        if day is not None:
            params["dates"] = f"{day:%Y%m%d}-{day:%Y%m%d}"
            if start and end:
                params["starttime"] = start
                params["endtime"] = end
        if extra:
            params.update(extra)
        response = self.request("GET", "objects.json", params=params, headers={"Accept": "application/json, text/javascript, */*"})
        if response.status_code in (401, 403, 412):
            raise NotLoggedIn(f"objects.json → HTTP {response.status_code}")
        if response.status_code != 200:
            raise RuntimeError(f"objects.json → HTTP {response.status_code}: {_squash(response.text)}")
        return parse_objects(response.text, default_type=str(types) if types is not None else None)

    def grid_html(self, objects: list[str], day: date, week_form: bool = False, today: date | None = None) -> str:
        """Fetch the week grid for ``objects`` covering ``day`` (HTML fragment)."""
        params: dict[str, Any] = {
            "h": "t",
            "sid": self.cfg.sid,
            "objects": ",".join(objects),
            "ox": 0,
            "types": 0,
            "fe": 0,
            "part": "t",
            "partajax": "t",
            "tg": -1,
            "se": "f",
            "exw": "t",
            "rr": 1,
        }
        if week_form:
            today = today or date.today()
            weeks = (day - day.weekday() * day.resolution).toordinal() - (today - today.weekday() * today.resolution).toordinal()
            weeks //= 7
            params["p"] = f"{weeks}.w,{weeks}.w"
        else:
            params["p"] = f"{day:%Y%m%d}-{day:%Y%m%d}"
        if self.cfg.locale:
            params["l"] = self.cfg.locale
        response = self.request("GET", "ri.html", params=params, headers={"Accept": "text/html, */*; q=0.01"})
        if response.status_code in (401, 403, 412):
            raise NotLoggedIn(f"ri.html → HTTP {response.status_code}")
        return response.text

    def reservation_form_html(self, room_object: str, day: date, start: str, end: str) -> str:
        """Fetch the "new reservation" form (step 2) as the UI does, for field discovery."""
        params: dict[str, Any] = {
            "h": "t",
            "sid": self.cfg.sid,
            "id": -1,
            "step": 2,
            "dates": f"{day:%Y%m%d}",
            "datesEnd": f"{day:%Y%m%d}",
            "startTime": start,
            "endTime": end,
            "o": room_object,
            "part": "t",
            "partajax": "t",
            "nocache": random.randint(1, 20),
        }
        if self.cfg.locale:
            params["l"] = self.cfg.locale
        response = self.request("GET", "ri.html", params=params, headers={"Accept": "text/html, */*; q=0.01"})
        return response.text


# -- parsing helpers --------------------------------------------------------

_LOGGED_IN_MARKERS = (
    "my reservations",
    "mine reservasjoner",
    "mine reservationer",
    "mina bokningar",
    "logged in as",
    "innlogget som",
    "inloggad som",
    "log out",
    "logg ut",
    "logga ut",
)
_LOGGED_OUT_MARKERS = ("logincontrol", "sso/", "log in", "logg inn", "logga in")


def looks_logged_in(html: str) -> bool:
    lowered = html.lower()
    if _contains(lowered, _LOGGED_IN_MARKERS):
        return True
    return False


def extract_reservation_id(text: str | None) -> str | None:
    if not text:
        return None
    match = re.search(r"[?&]id=(\d+)", text)
    if match:
        return match.group(1)
    match = re.search(r"\bid[=:]\s*\"?(\d+)", text)
    return match.group(1) if match else None


def parse_objects(text: str, default_type: str | None = None) -> list[TEObject]:
    """Parse ``objects.json`` output in the shapes seen across TimeEdit versions."""
    text = text.strip()
    if not text or text.startswith('"'):  # e.g. "Inga sökresultat" / "No search results"
        return []
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if isinstance(data, list):
        items = data
    else:
        items = data.get("objects") or data.get("records") or []
    result: list[TEObject] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        raw_id = str(item.get("idAndType") or item.get("id") or "")
        if not raw_id:
            continue
        if "." in raw_id:
            obj_id, obj_type = raw_id.split(".", 1)
        else:
            obj_id, obj_type = raw_id, str(item.get("type") or default_type or "")
        fields = item.get("fields") or {}
        name = _best_name(fields, fallback=str(item.get("name") or raw_id))
        result.append(TEObject(obj_id, obj_type, name, fields if isinstance(fields, dict) else {"values": fields}))
    return result


def _flatten_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for item in value.values():
            out.extend(_flatten_strings(item))
        return out
    if isinstance(value, list):
        out = []
        for item in value:
            out.extend(_flatten_strings(item))
        return out
    return []


def _best_name(fields: Any, fallback: str) -> str:
    strings = [s.strip() for s in _flatten_strings(fields) if s and s.strip()]
    if not strings:
        return fallback
    for candidate in strings:  # prefer something that looks like a room name
        if re.search(r"(?i)\b(grupperom|rom|room|grupprum|sal)\b", candidate):
            return candidate
    return strings[0]


def normalise_reservation(raw: dict[str, Any]) -> dict[str, Any]:
    """Pull date/time/room out of a ``my.json`` reservation dict (best effort)."""
    out: dict[str, Any] = {"raw": raw, "id": str(raw.get("id") or raw.get("reservationid") or "")}
    text = json.dumps(raw, ensure_ascii=False)
    for key in ("startdate", "startDate", "start_date", "date", "dates"):
        if raw.get(key):
            out["date"] = str(raw[key])[:10]
            break
    for key in ("starttime", "startTime", "start_time", "start"):
        if raw.get(key):
            out["start"] = str(raw[key])[-8:]
            break
    for key in ("endtime", "endTime", "end_time", "end"):
        if raw.get(key):
            out["end"] = str(raw[key])[-8:]
            break
    if "date" not in out:
        match = re.search(r"(\d{4}-\d{2}-\d{2}|\d{8})", text)
        if match:
            value = match.group(1)
            out["date"] = value if "-" in value else f"{value[:4]}-{value[4:6]}-{value[6:]}"
    if "start" not in out:
        match = re.search(r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", text)
        if match:
            out["start"], out["end"] = match.group(1), match.group(2)
    room = None
    for value in _flatten_strings(raw):
        if re.search(r"(?i)\b(grupperom|grupprum|room|rom)\b", value):
            room = value
            break
    out["room"] = room
    return out


def query_fields(url: str) -> dict[str, str]:
    """Return the query parameters of ``url`` as a dict (last value wins)."""
    return dict(parse_qsl(urlparse(url).query, keep_blank_values=True))
