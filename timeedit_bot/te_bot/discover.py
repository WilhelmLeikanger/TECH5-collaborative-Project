"""Discover instance-specific ids without human help.

* ``sid``        – decoded from the reservation page key (``ri1Q56`` → 12)
* room type id   – the object type that dominates the reservation page grid
* rooms          – ``objects.json`` search (names + ids)
* form fields    – the "new reservation" form (``step=2``) input names

Everything found is written to ``discovered.json``; the raw HTML responses
are kept in the captures folder so a human can look at them when something
does not match.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any

from .config import Config
from .schedule_parser import parse_grid, parse_html
from .scramble import sid_from_key
from .timeedit import TEObject, TimeEditClient, looks_logged_in

log = logging.getLogger("te_bot.discover")

_ATTR_ID_RE = re.compile(r"(?:data-object|data-id|data-idandtype|data-idAndType)=\"(\d+)\.(\d+)\"")
_ANY_ID_RE = re.compile(r"(?<![\d.])(\d{2,8})\.(\d{2,4})(?![\d.])")
_SID_RE = re.compile(r"[?&;]sid=(\d+)")


def _form_fields(html: str) -> list[dict[str, Any]]:
    root = parse_html(html)
    out: list[dict[str, Any]] = []
    for node in root.find_all():
        if node.tag not in ("input", "select", "textarea"):
            continue
        name = node.attrs.get("name")
        if not name:
            continue
        entry: dict[str, Any] = {
            "tag": node.tag,
            "name": name,
            "type": node.attrs.get("type", ""),
            "value": node.attrs.get("value", ""),
            "required": "required" in node.attrs,
        }
        if node.tag == "select":
            entry["options"] = [
                {"value": opt.attrs.get("value", ""), "text": opt.inner_text(), "selected": "selected" in opt.attrs}
                for opt in node.find_all("option")
            ]
        out.append(entry)
    return out


def _rooms_from_page(html: str, room_type: str | None, name_regex: re.Pattern[str]) -> list[TEObject]:
    """Fallback: pair ``data-object`` ids with nearby room names in the page."""
    root = parse_html(html)
    found: dict[str, TEObject] = {}
    for node in root.find_all(attr="data-object"):
        obj = node.attrs.get("data-object", "")
        if "." not in obj:
            continue
        obj_id, obj_type = obj.split(".", 1)
        if room_type and obj_type != room_type:
            continue
        name = node.attrs.get("data-name") or node.attrs.get("title") or node.inner_text()
        if not name or not name_regex.search(name):
            continue
        found.setdefault(obj, TEObject(obj_id, obj_type, name.strip()))
    return list(found.values())


def discover(cfg: Config, client: TimeEditClient) -> dict[str, Any]:
    cfg.ensure_dirs()
    notes: list[str] = []
    result: dict[str, Any] = {"discovered_at": datetime.now().isoformat(timespec="seconds")}
    name_regex = re.compile(cfg.timeedit.room_name_regex)

    # 1. The reservation page itself ---------------------------------------
    html = client.fetch_reserve_page()
    (cfg.capture_dir / "reserve_page.html").write_text(html, encoding="utf-8")
    result["logged_in"] = looks_logged_in(html)
    if not result["logged_in"]:
        notes.append("Reservation page does not look logged in — run 'te_bot login' first")

    key_sid = sid_from_key(cfg.timeedit.reserve_page)
    page_sids = Counter(int(s) for s in _SID_RE.findall(html))
    result["sid_from_key"] = key_sid
    result["sids_in_page"] = dict(page_sids.most_common(5))
    sid = cfg.timeedit.sid
    if key_sid and key_sid != sid:
        notes.append(f"sid in config is {sid} but the page key decodes to {key_sid}")
    result["sid"] = sid

    attr_ids = Counter(f"{a}.{b}" for a, b in _ATTR_ID_RE.findall(html))
    any_ids = Counter(f"{a}.{b}" for a, b in _ANY_ID_RE.findall(html))
    type_counts = Counter(obj.split(".")[1] for obj in (attr_ids or any_ids))
    room_type = str(cfg.timeedit.room_type) if cfg.timeedit.room_type else None
    if not room_type and type_counts:
        room_type = type_counts.most_common(1)[0][0]
        notes.append(f"Guessed room type {room_type} from {sum(type_counts.values())} object ids in the page")
    result["object_types_in_page"] = dict(type_counts.most_common(5))
    result["room_type"] = int(room_type) if room_type and room_type.isdigit() else None

    # 2. Rooms via objects.json ---------------------------------------------
    rooms: list[TEObject] = []
    attempts: list[tuple[str, dict[str, Any]]] = []
    if room_type:
        attempts.append(("objects.json types=%s" % room_type, {"types": room_type}))
    attempts.append(("objects.json (no type filter)", {}))
    attempts.append(("objects.json search_text=Grupperom", {"search_text": "Grupperom"}))
    for label, kwargs in attempts:
        try:
            found = client.objects(max_results=300, **kwargs)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"{label} failed: {exc}")
            continue
        matching = [o for o in found if name_regex.search(o.name)]
        log.info("%s → %d objects, %d match %r", label, len(found), len(matching), cfg.timeedit.room_name_regex)
        if matching:
            rooms = matching
            result["rooms_source"] = label
            break
    if not rooms:
        rooms = _rooms_from_page(html, room_type, name_regex)
        if rooms:
            result["rooms_source"] = "reserve page markup"
            notes.append("Rooms taken from page markup; objects.json returned nothing usable")
    if rooms and not room_type:
        room_type = Counter(r.type for r in rooms).most_common(1)[0][0]
        result["room_type"] = int(room_type)
    result["rooms"] = [{"id": r.id, "type": r.type, "name": r.name} for r in rooms]
    if not rooms:
        notes.append("No rooms discovered — run 'te_bot learn' and/or inspect captures/reserve_page.html")

    # 3. The reservation form (step 2) -------------------------------------
    probe_day = date.today() + timedelta(days=1)
    if rooms:
        try:
            form_html = client.reservation_form_html(rooms[0].object_id, probe_day, "09:00", "10:00")
            (cfg.capture_dir / "reservation_form.html").write_text(form_html, encoding="utf-8")
            fields = _form_fields(form_html)
            result["form_fields"] = fields
            fe_fields = [f["name"] for f in fields if f["name"].startswith("fe")]
            if fe_fields:
                notes.append("Form has free-text fields: " + ", ".join(fe_fields))
        except Exception as exc:  # noqa: BLE001
            notes.append(f"Could not fetch reservation form: {exc}")

    # 4. The grid for a day in the open window ------------------------------
    if rooms:
        try:
            grid_html = client.grid_html([r.object_id for r in rooms[:40]], probe_day)
            (cfg.capture_dir / "grid.html").write_text(grid_html, encoding="utf-8")
            grid = parse_grid(grid_html)
            if not grid.days:
                grid_html = client.grid_html([r.object_id for r in rooms[:40]], probe_day, week_form=True)
                grid = parse_grid(grid_html)
                if grid.days:
                    result["grid_week_form"] = True
            result["grid"] = {"days": grid.days, "rooms": len(grid.rooms), "bookings": len(grid.bookings)}
            if not grid.days:
                notes.append("Week grid could not be parsed; availability pre-check will be skipped (bookings still work)")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"Grid fetch failed: {exc}")

    result["notes"] = notes
    cfg.discovered_file.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Discovery written to %s", cfg.discovered_file)
    return result


def load_discovered(cfg: Config) -> dict[str, Any] | None:
    if not cfg.discovered_file.exists():
        return None
    try:
        return json.loads(cfg.discovered_file.read_text(encoding="utf-8"))
    except ValueError:
        return None


def rooms_from_discovered(data: dict[str, Any] | None) -> list[TEObject]:
    if not data:
        return []
    return [TEObject(str(r["id"]), str(r["type"]), str(r["name"])) for r in data.get("rooms", [])]


_SET_BY_BOT = {"kind", "nocache", "l", "aos", "dates", "starttime", "endtime", "startTime", "endTime", "url", "CSTT", "CSTTG", "id", "o"}


def default_fields_from_discovery(data: dict[str, Any] | None) -> tuple[dict[str, str], list[str]]:
    """Derive default form fields and extra ``o`` objects from the discovered form.

    Returns ``(fields, extra_objects)`` where ``fields`` are the free-text /
    hidden inputs with their default values and ``extra_objects`` are hidden
    ``o`` inputs that are not rooms (e.g. the person making the booking).
    """
    fields: dict[str, str] = {}
    extra: list[str] = []
    if not data:
        return fields, extra
    room_type = str(data.get("room_type") or "")
    for entry in data.get("form_fields", []):
        name = str(entry.get("name") or "")
        if not name:
            continue
        if name == "o":
            value = str(entry.get("value") or "")
            if value and "." in value and not value.endswith("." + room_type) and value not in extra:
                extra.append(value)
            continue
        if name in _SET_BY_BOT or name in fields:
            continue
        if entry.get("tag") == "select":
            options = entry.get("options") or []
            chosen = next((o for o in options if o.get("selected")), None)
            value = str((chosen or (options[0] if options else {})).get("value", ""))
        else:
            value = str(entry.get("value") or "")
        fields[name] = value
    return fields, extra
