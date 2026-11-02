"""Configuration for the TimeEdit room-sniper bot.

Configuration lives in a TOML file (default ``~/.timeedit-bot/config.toml``).
Every key has a sensible default for NHH's group-room reservation page, so an
empty file is a valid configuration.  See ``config.example.toml`` for the
documented template.
"""

from __future__ import annotations

import dataclasses
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    import tomli as tomllib  # type: ignore[no-redef]

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
WINDOW_RE = re.compile(r"^(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})$")

DEFAULT_HOME = Path(os.environ.get("TE_BOT_HOME", "~/.timeedit-bot")).expanduser()


class ConfigError(ValueError):
    """Raised for invalid configuration values."""


def parse_hhmm(value: str) -> int:
    """Parse ``HH:MM`` into minutes since midnight."""
    match = TIME_RE.match(value.strip())
    if not match:
        raise ConfigError(f"Invalid time {value!r}; expected HH:MM")
    hours, minutes = int(match.group(1)), int(match.group(2))
    if not (0 <= hours <= 24 and 0 <= minutes < 60):
        raise ConfigError(f"Invalid time {value!r}")
    return hours * 60 + minutes


def fmt_hhmm(minutes: int) -> str:
    """Format minutes since midnight as zero-padded ``HH:MM``."""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass
class TimeEditConfig:
    """Where the reservation page lives and how to talk to it."""

    base_url: str = "https://cloud.timeedit.net/nhh/web/schedule"
    reserve_page: str = "ri1Q56.html"
    #: Search/reservation id encoded in ``ri1Q56`` (``ri.html?h=t&sid=12``).
    sid: int = 12
    #: Value for TimeEdit's ``l`` (locale) parameter; empty string omits it.
    locale: str = "en_GB"
    timezone: str = "Europe/Oslo"
    #: Object-type id of rooms (e.g. 186).  ``None`` until discovered.
    room_type: int | None = None
    #: Regex that room names must match to be considered "group rooms".
    room_name_regex: str = r"^Grupperom\b"
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    )
    request_timeout: float = 10.0

    @property
    def reserve_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/{self.reserve_page}"

    @property
    def origin(self) -> str:
        match = re.match(r"^(https?://[^/]+)", self.base_url)
        if not match:
            raise ConfigError(f"base_url must be absolute: {self.base_url!r}")
        return match.group(1)

    @property
    def web_root(self) -> str:
        """``https://cloud.timeedit.net/nhh/web`` (parent of the type segment)."""
        return self.base_url.rstrip("/").rsplit("/", 1)[0]

    @property
    def org(self) -> str:
        """Organisation slug (``nhh``)."""
        return self.web_root.rstrip("/").rsplit("/", 2)[-2]


@dataclass
class ReleaseConfig:
    """When new days open for booking and how aggressively to fire."""

    time: str = "21:00"
    days_ahead: int = 14
    #: Fire the first attempt this many ms *before* the release instant.
    lead_ms: int = 40
    #: Retry interval while the slot is "not open yet" (burst phase).
    fast_retry_ms: int = 150
    burst_seconds: int = 45
    #: Retry interval after the burst phase.
    slow_retry_s: float = 2.0
    #: Give up this long after the release instant.
    max_wait_seconds: int = 600
    #: Start preparations (session check, discovery, CSRF) this early.
    prepare_minutes: int = 10
    clock_sync: bool = True
    #: Never send requests faster than this, whatever else happens.
    min_request_gap_ms: int = 100


@dataclass
class StrategyConfig:
    """Which rooms/time windows to go for, in priority order."""

    rooms: list[str] = field(
        default_factory=lambda: [
            "Grupperom U28",
            "Grupperom U30",
            "Grupperom U29",
            "Grupperom U31",
            "Grupperom U32",
            "Grupperom U20",
        ]
    )
    exclude_rooms: list[str] = field(default_factory=list)
    #: After the priority list, try every other room matching room_name_regex.
    any_room_fallback: bool = True
    windows: list[str] = field(
        default_factory=lambda: [
            "08:00-12:00",
            "12:00-16:00",
            "09:00-13:00",
            "13:00-17:00",
            "10:00-14:00",
        ]
    )
    max_hours: float = 4.0
    min_hours: float = 1.0
    duration_step_hours: float = 1.0
    fallback_shorter: bool = True
    #: ``room-first``: exhaust every window for room #1 before trying room #2.
    #: ``window-first``: try window #1 in every room before window #2.
    order: str = "room-first"
    weekdays: list[str] = field(default_factory=lambda: ["mon", "tue", "wed", "thu"])
    weekly_quota: int = 4
    blocks_per_day: int = 1
    #: Free-text fields (``fe<N>``) to send with the reservation, e.g. {"fe2": "Group work"}.
    fields: dict[str, str] = field(default_factory=dict)

    def window_minutes(self) -> list[tuple[int, int]]:
        out: list[tuple[int, int]] = []
        for window in self.windows:
            match = WINDOW_RE.match(window)
            if not match:
                raise ConfigError(f"Invalid window {window!r}; expected HH:MM-HH:MM")
            start, end = parse_hhmm(match.group(1)), parse_hhmm(match.group(2))
            if end <= start:
                raise ConfigError(f"Window {window!r} must end after it starts")
            out.append((start, end))
        return out


@dataclass
class AuthConfig:
    """How to obtain a logged-in TimeEdit session."""

    #: ``browser``: open a window and let a human log in (Feide/MFA friendly).
    #: ``feide``: fully automated Feide login with credentials from env vars.
    method: str = "browser"
    username_env: str = "FEIDE_USERNAME"
    password_env: str = "FEIDE_PASSWORD"
    #: Text typed into Feide's organisation chooser.
    feide_org: str = "NHH"
    headless: bool = True
    #: Re-login proactively if the saved session is older than this many hours.
    max_session_age_hours: float = 20.0


@dataclass
class Config:
    timeedit: TimeEditConfig = field(default_factory=TimeEditConfig)
    release: ReleaseConfig = field(default_factory=ReleaseConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    home: Path = field(default_factory=lambda: DEFAULT_HOME)
    source: Path | None = None

    # Derived file locations -------------------------------------------------
    @property
    def state_file(self) -> Path:
        return self.home / "state.json"

    @property
    def storage_state_file(self) -> Path:
        return self.home / "storage_state.json"

    @property
    def template_file(self) -> Path:
        return self.home / "template.json"

    @property
    def discovered_file(self) -> Path:
        return self.home / "discovered.json"

    @property
    def profile_dir(self) -> Path:
        return self.home / "browser-profile"

    @property
    def log_dir(self) -> Path:
        return self.home / "logs"

    @property
    def capture_dir(self) -> Path:
        return self.home / "captures"

    def ensure_dirs(self) -> None:
        for path in (self.home, self.log_dir, self.capture_dir):
            path.mkdir(parents=True, exist_ok=True)
        try:  # keep cookies/credentials private on POSIX
            os.chmod(self.home, 0o700)
        except OSError:  # pragma: no cover - Windows
            pass


def _apply(section: Any, data: dict[str, Any], name: str) -> None:
    known = {f.name for f in dataclasses.fields(section)}
    for key, value in data.items():
        if key not in known:
            raise ConfigError(f"Unknown key [{name}].{key}")
        setattr(section, key, value)


def validate(cfg: Config) -> Config:
    """Validate cross-field constraints; returns the config for chaining."""
    parse_hhmm(cfg.release.time)
    if cfg.release.days_ahead < 0:
        raise ConfigError("release.days_ahead must be >= 0")
    strat = cfg.strategy
    strat.window_minutes()
    if strat.order not in ("room-first", "window-first"):
        raise ConfigError("strategy.order must be 'room-first' or 'window-first'")
    for day in strat.weekdays:
        if day.lower()[:3] not in WEEKDAYS:
            raise ConfigError(f"Unknown weekday {day!r}")
    strat.weekdays = [d.lower()[:3] for d in strat.weekdays]
    if strat.min_hours <= 0 or strat.max_hours < strat.min_hours:
        raise ConfigError("strategy.min_hours/max_hours are inconsistent")
    if strat.duration_step_hours <= 0:
        raise ConfigError("strategy.duration_step_hours must be > 0")
    if strat.blocks_per_day < 1:
        raise ConfigError("strategy.blocks_per_day must be >= 1")
    if not strat.rooms and not strat.any_room_fallback:
        raise ConfigError("No rooms configured and any_room_fallback is false")
    if cfg.auth.method not in ("browser", "feide"):
        raise ConfigError("auth.method must be 'browser' or 'feide'")
    re.compile(cfg.timeedit.room_name_regex)
    return cfg


def from_dict(data: dict[str, Any], home: Path | None = None) -> Config:
    cfg = Config(home=home or DEFAULT_HOME)
    sections = {
        "timeedit": cfg.timeedit,
        "release": cfg.release,
        "strategy": cfg.strategy,
        "auth": cfg.auth,
    }
    for name, value in data.items():
        if name == "home":
            cfg.home = Path(str(value)).expanduser()
            continue
        if name not in sections:
            raise ConfigError(f"Unknown section [{name}]")
        if not isinstance(value, dict):
            raise ConfigError(f"[{name}] must be a table")
        _apply(sections[name], value, name)
    return validate(cfg)


def load_config(path: str | Path | None = None, home: Path | None = None) -> Config:
    """Load the TOML config at ``path`` (or the default location).

    A missing file yields the defaults so ``te_bot`` works out of the box.
    """
    home = home or DEFAULT_HOME
    env_path = os.environ.get("TE_BOT_CONFIG")
    candidate = Path(path or env_path or (home / "config.toml")).expanduser()
    if candidate.exists():
        with candidate.open("rb") as handle:
            data = tomllib.load(handle)
        cfg = from_dict(data, home=home)
        cfg.source = candidate
        return cfg
    if path or env_path:
        raise ConfigError(f"Config file not found: {candidate}")
    return validate(Config(home=home))
