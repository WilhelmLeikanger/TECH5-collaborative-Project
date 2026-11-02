"""Learn the exact reservation request by watching one manual booking.

Every TimeEdit instance has its own set of extra form fields (``fe<N>``
free-text fields, an extra ``o`` object for the person making the booking,
etc.).  Rather than guessing, ``te_bot learn`` opens a browser, lets you make
one ordinary reservation by hand and records the request the page sends.  The
recorded body becomes ``template.json``; from then on the bot only swaps the
room, date, times and CSRF token.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlparse

from .auth import _open_context, _playwright, save_storage_state
from .config import Config

log = logging.getLogger("te_bot.learn")

_ID_TYPE_RE = re.compile(r"(?<![\d.])(\d{2,8})\.(\d{2,4})(?![\d.])")


def _parse_post(post: str) -> list[tuple[str, str]]:
    return [(k, v) for k, v in parse_qsl(post, keep_blank_values=True)]


def guess_room_object(fields: list[tuple[str, str]], page_html: str, known_rooms: dict[str, str] | None = None) -> str | None:
    """Pick which ``o`` value is the room (as opposed to e.g. the person)."""
    objects = [v for k, v in fields if k == "o"]
    if not objects:
        return None
    if len(objects) == 1:
        return objects[0]
    known_rooms = known_rooms or {}
    for obj in objects:
        if obj in known_rooms:
            return obj
    counts = Counter(f"{a}.{b}" for a, b in _ID_TYPE_RE.findall(page_html))
    type_counts = Counter(obj.split(".")[-1] for obj in counts)
    # The room type is the type that appears most often in the grid.
    best = None
    best_score = -1
    for obj in objects:
        score = counts.get(obj, 0) * 1000 + type_counts.get(obj.split(".")[-1], 0)
        if score > best_score:
            best, best_score = obj, score
    return best


def learn(cfg: Config, timeout_s: int = 900, ask: Any = input, on_page: Any = None, headless: bool = False) -> dict[str, Any]:
    """Record one manual reservation and write ``template.json``.

    ``on_page`` is an optional callback invoked with the page once it is open
    (used by the tests to script the "manual" booking).
    """
    sync_playwright = _playwright()
    host = urlparse(cfg.timeedit.base_url).hostname or ""
    cfg.ensure_dirs()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    har_path = cfg.capture_dir / f"learn-{stamp}.har"
    captured: dict[str, Any] = {}
    hints: dict[str, Any] = {"objects_requests": [], "csrf_requests": [], "grid_requests": []}

    def on_response(response: Any) -> None:
        try:
            request = response.request
            if host not in request.url:
                return
            url = request.url
            path = urlparse(url).path
            if request.method == "GET" and path.endswith("objects.json"):
                hints["objects_requests"].append(url)
            elif request.method == "GET" and path.endswith("ri.html") and "partajax" in url:
                hints["grid_requests"].append(url)
            if request.method != "POST":
                return
            post = request.post_data or ""
            if "CSTTG=gen" in post:
                hints["csrf_requests"].append(url)
                return
            if "kind=reserve" not in post:
                return
            fields = _parse_post(post)
            try:
                body = response.text()
            except Exception:  # noqa: BLE001 - redirects have no body
                body = ""
            headers = {k: v for k, v in request.headers.items() if k.lower() not in ("cookie", "content-length")}
            captured.update(
                {
                    "url": url,
                    "method": request.method,
                    "headers": headers,
                    "fields": fields,
                    "response": {
                        "status": response.status,
                        "location": response.headers.get("location"),
                        "body": body[:4000],
                    },
                    "captured_at": datetime.now().isoformat(timespec="seconds"),
                }
            )
            log.info("Captured reservation request → HTTP %s", response.status)
        except Exception as exc:  # noqa: BLE001
            log.debug("on_response: %s", exc)

    with sync_playwright() as p:
        context = _open_context(
            p,
            cfg,
            headless=headless,
            record_har_path=str(har_path),
            record_har_content="embed",
            record_har_url_filter=f"**{host}**",
        )
        context.on("response", on_response)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(cfg.timeedit.reserve_url, wait_until="domcontentloaded")
        if on_page is not None:
            on_page(page)
        print()
        print("=" * 70)
        print("LEARN MODE — a browser window is open.")
        print("1. Log in if you are not logged in already.")
        print("2. Make ONE ordinary reservation by hand (any free 1-hour slot in a")
        print("   group room is fine — you can cancel it afterwards).")
        print("3. As soon as the reservation is confirmed, this window closes itself.")
        print("=" * 70)
        deadline = time.time() + timeout_s
        page_html = ""
        while time.time() < deadline:
            if captured:
                time.sleep(1.5)  # let the page finish its follow-up requests
                try:
                    page_html = context.pages[-1].content()
                except Exception:  # noqa: BLE001
                    page_html = ""
                break
            if not context.pages:
                raise RuntimeError("Browser window closed before a reservation was captured")
            time.sleep(0.5)
        state = context.storage_state()
        context.close()  # flushes the HAR
    save_storage_state(cfg, state)
    if not captured:
        raise RuntimeError(f"No reservation request captured within {timeout_s}s (HAR saved to {har_path})")

    fields = captured["fields"]
    status = captured["response"].get("status")
    if status not in (200, 302, 303):
        log.warning(
            "The captured reservation was rejected by TimeEdit (HTTP %s: %s). The field layout is still "
            "recorded, but make sure a booking actually succeeds before relying on it.",
            status,
            (captured["response"].get("body") or "")[:160],
        )
    objects = [v for k, v in fields if k == "o"]
    room = guess_room_object(fields, page_html)
    if len(objects) > 1 and ask is not None:
        print("\nThe reservation sent several objects:")
        for i, obj in enumerate(objects, 1):
            print(f"  {i}. {obj}{'  (guess: the room)' if obj == room else ''}")
        answer = ask("Which one is the ROOM you booked? [number, Enter = keep guess]: ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(objects):
            room = objects[int(answer) - 1]
    template = {
        "url": captured["url"],
        "method": captured["method"],
        "headers": captured["headers"],
        "fields": fields,
        "room_object": room,
        "room_type": room.split(".")[-1] if room else None,
        "response": captured["response"],
        "hints": hints,
        "captured_at": captured["captured_at"],
        "har": str(har_path),
    }
    cfg.template_file.write_text(json.dumps(template, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Template written to %s", cfg.template_file)
    return template


def load_template(cfg: Config) -> dict[str, Any] | None:
    if not cfg.template_file.exists():
        return None
    try:
        return json.loads(cfg.template_file.read_text(encoding="utf-8"))
    except ValueError:
        log.warning("template.json is corrupt; ignoring")
        return None


def describe_template(template: dict[str, Any]) -> str:
    lines = [f"captured {template.get('captured_at')} → {template.get('url')}"]
    for key, value in template.get("fields", []):
        marker = ""
        if key == "o" and value == template.get("room_object"):
            marker = "   ← room (replaced per attempt)"
        elif key in ("dates", "starttime", "endtime", "CSTT", "nocache"):
            marker = "   ← replaced per attempt"
        lines.append(f"  {key} = {value!r}{marker}")
    resp = template.get("response") or {}
    lines.append(f"  response: HTTP {resp.get('status')} location={resp.get('location')}")
    return "\n".join(lines)
