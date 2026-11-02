"""Persistent state (bookings made by the bot) and logging setup."""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .config import Config


class State:
    """Small JSON document in ``~/.timeedit-bot/state.json``."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {"bookings": [], "runs": []}
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except ValueError:
                backup = self.path.with_suffix(".corrupt.json")
                self.path.replace(backup)
        self.data.setdefault("bookings", [])
        self.data.setdefault("runs", [])

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    @property
    def bookings(self) -> list[dict[str, Any]]:
        return self.data["bookings"]

    def add_booking(self, day: date, room: str, object_id: str, start: str, end: str, reservation_id: str | None) -> None:
        self.bookings.append(
            {
                "date": day.isoformat(),
                "room": room,
                "object": object_id,
                "start": start,
                "end": end,
                "id": reservation_id,
                "made_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
        self.save()

    def record_run(self, summary: dict[str, Any], keep: int = 60) -> None:
        self.data["runs"].append(summary)
        self.data["runs"] = self.data["runs"][-keep:]
        self.save()

    def upcoming(self, today: date | None = None) -> list[dict[str, Any]]:
        today = today or date.today()
        return [b for b in self.bookings if b.get("date", "") >= today.isoformat()]


def setup_logging(cfg: Config, verbose: bool = False) -> logging.Logger:
    cfg.ensure_dirs()
    root = logging.getLogger("te_bot")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    if root.handlers:
        return root
    fmt = logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)
    file_handler = logging.handlers.RotatingFileHandler(
        cfg.log_dir / "te_bot.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    file_handler.setLevel(logging.DEBUG)
    root.addHandler(file_handler)
    root.propagate = False
    return root
