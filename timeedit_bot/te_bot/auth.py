"""Obtain and persist a logged-in TimeEdit session using a real browser.

NHH authenticates through Feide (SAML).  Doing that by hand once in a
Playwright-controlled Chromium is the most robust option: the resulting
cookies are saved to ``storage_state.json`` and reused by the fast HTTP
client.  An automated Feide login (username/password from environment
variables) is available for unattended machines.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from typing import Any, Callable

from .config import Config
from .timeedit import TimeEditClient, looks_logged_in

log = logging.getLogger("te_bot.auth")

LOGIN_LINK_SELECTORS = [
    "#logincontrol a",
    "a[href*='/sso/']",
    "a[href*='sso']",
    "a:has-text('Log in')",
    "a:has-text('Logg inn')",
    "a:has-text('Login')",
    "button:has-text('Log in')",
    "button:has-text('Logg inn')",
]
ORG_SEARCH_SELECTORS = [
    "#org_selector-selectized",
    "input#org-search",
    "input[name='org']",
    "input[type='search']",
    "input[placeholder*='Søk']",
    "input[placeholder*='Search']",
    "input[placeholder*='organisasjon']",
    "input[placeholder*='organization']",
    "input[placeholder*='institution']",
    ".selectize-input input",
]
USERNAME_SELECTORS = ["#username", "input[name='feidename']", "input[name='username']", "input[autocomplete='username']", "input[type='text']"]
PASSWORD_SELECTORS = ["#password", "input[name='password']", "input[type='password']"]
SUBMIT_SELECTORS = [
    "button[type='submit']",
    "input[type='submit']",
    "#submit",
    "button:has-text('Logg inn')",
    "button:has-text('Log in')",
    "button:has-text('Login')",
]
CONTINUE_SELECTORS = [
    "button:has-text('Fortsett')",
    "button:has-text('Continue')",
    "button:has-text('Yes, continue')",
    "button:has-text('Ja, fortsett')",
    "input[value='Fortsett']",
    "input[value='Continue']",
]
ERROR_MARKERS = ("feil brukernavn", "wrong username", "incorrect", "ugyldig", "invalid", "mislyktes", "failed")


def _playwright() -> Any:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise RuntimeError(
            "Playwright is not installed. Run: pip install playwright && playwright install chromium"
        ) from exc
    return sync_playwright


def save_storage_state(cfg: Config, state: dict[str, Any]) -> None:
    cfg.ensure_dirs()
    cfg.storage_state_file.write_text(json.dumps(state, indent=1), encoding="utf-8")
    try:
        os.chmod(cfg.storage_state_file, 0o600)
    except OSError:  # pragma: no cover
        pass
    log.info("Saved session cookies to %s", cfg.storage_state_file)


def load_storage_state(cfg: Config) -> dict[str, Any] | None:
    if not cfg.storage_state_file.exists():
        return None
    try:
        return json.loads(cfg.storage_state_file.read_text(encoding="utf-8"))
    except ValueError:
        log.warning("Corrupt storage state at %s; ignoring", cfg.storage_state_file)
        return None


def storage_age_hours(cfg: Config) -> float | None:
    if not cfg.storage_state_file.exists():
        return None
    return (time.time() - cfg.storage_state_file.stat().st_mtime) / 3600.0


def _first_visible(page: Any, selectors: list[str], timeout_ms: int = 300) -> Any | None:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() and locator.is_visible(timeout=timeout_ms):
                return locator
        except Exception:  # noqa: BLE001 - selector may be invalid on this page
            continue
    return None


def _snapshot(cfg: Config, page: Any, tag: str) -> None:
    try:
        cfg.capture_dir.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(cfg.capture_dir / f"login-{tag}.png"), full_page=False)
    except Exception:  # noqa: BLE001
        pass


def _is_logged_in(cfg: Config, context: Any, page: Any) -> bool:
    try:
        if not page.url.startswith(cfg.timeedit.origin):
            return False
        if looks_logged_in(page.content()):
            return True
    except Exception:  # noqa: BLE001
        return False
    # Fall back to a real HTTP check with the browser's cookies.
    client = TimeEditClient(cfg.timeedit, context.cookies())
    try:
        return client.is_logged_in()
    except Exception:  # noqa: BLE001
        return False


def _restore_cookies(cfg: Config, context: Any) -> None:
    """Inject saved cookies into a fresh context (Chromium drops session cookies between launches)."""
    state = load_storage_state(cfg)
    if not state:
        return
    cookies = []
    for raw in state.get("cookies", []):
        if not raw.get("name") or not raw.get("domain"):
            continue
        cookie = {"name": raw["name"], "value": raw.get("value", ""), "domain": raw["domain"], "path": raw.get("path", "/")}
        for key in ("expires", "httpOnly", "secure", "sameSite"):
            if key in raw:
                cookie[key] = raw[key]
        cookies.append(cookie)
    if cookies:
        try:
            context.add_cookies(cookies)
        except Exception as exc:  # noqa: BLE001
            log.debug("Could not restore cookies: %s", exc)


def _open_context(p: Any, cfg: Config, headless: bool, **kwargs: Any) -> Any:
    cfg.ensure_dirs()
    try:
        context = p.chromium.launch_persistent_context(
            str(cfg.profile_dir),
            headless=headless,
            viewport={"width": 1280, "height": 900},
            locale="en-GB",
            timezone_id=cfg.timeedit.timezone,
            **kwargs,
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"Could not start Chromium with profile {cfg.profile_dir}: {exc}\n"
            "Is another te_bot browser (login/learn) still open? Did you run 'playwright install chromium'?"
        ) from exc
    _restore_cookies(cfg, context)
    return context


def interactive_login(cfg: Config, timeout_s: int = 900) -> dict[str, Any]:
    """Open a browser window, let the user log in, save and return the cookies."""
    sync_playwright = _playwright()
    with sync_playwright() as p:
        context = _open_context(p, cfg, headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(cfg.timeedit.reserve_url, wait_until="domcontentloaded")
        time.sleep(1.0)
        if not _is_logged_in(cfg, context, page):
            link = _first_visible(page, LOGIN_LINK_SELECTORS)
            if link is not None:
                try:
                    link.click()
                except Exception:  # noqa: BLE001
                    pass
        print()
        print("=" * 70)
        print("A browser window is open. Log in to TimeEdit with Feide (NHH).")
        print("When the page shows 'My reservations', this window closes by itself.")
        print("=" * 70)
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                if _is_logged_in(cfg, context, page):
                    break
                if not context.pages:
                    raise RuntimeError("Browser window was closed before login completed")
                page = context.pages[-1]
            except RuntimeError:
                raise
            except Exception:  # noqa: BLE001 - navigation in progress
                pass
            time.sleep(1.0)
        else:
            raise RuntimeError("Timed out waiting for login")
        try:
            page.goto(cfg.timeedit.reserve_url, wait_until="domcontentloaded")
            time.sleep(0.5)
        except Exception:  # noqa: BLE001
            pass
        state = context.storage_state()
        context.close()
    save_storage_state(cfg, state)
    return state


def automated_feide_login(cfg: Config, username: str, password: str, timeout_s: int = 180, headless: bool | None = None) -> dict[str, Any]:
    """Drive the Feide login form without a human (best effort, screenshots on the way)."""
    sync_playwright = _playwright()
    headless = cfg.auth.headless if headless is None else headless
    with sync_playwright() as p:
        context = _open_context(p, cfg, headless=headless)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(cfg.timeedit.reserve_url, wait_until="domcontentloaded")
        time.sleep(1.0)
        if _is_logged_in(cfg, context, page):
            state = context.storage_state()
            context.close()
            save_storage_state(cfg, state)
            return state
        link = _first_visible(page, LOGIN_LINK_SELECTORS)
        if link is not None:
            link.click()
        else:
            # Try TimeEdit's conventional SSO entry point.
            page.goto(f"{cfg.timeedit.web_root}/timeedit/sso/feide?back={cfg.timeedit.reserve_url}", wait_until="domcontentloaded")
        deadline = time.time() + timeout_s
        step = 0
        typed_org = typed_user = typed_pass = False
        while time.time() < deadline:
            step += 1
            time.sleep(1.0)
            try:
                if not context.pages:
                    raise RuntimeError("Browser closed")
                page = context.pages[-1]
                _snapshot(cfg, page, f"{step:02d}")
                if _is_logged_in(cfg, context, page):
                    break
                content = page.content().lower()
                if any(marker in content for marker in ERROR_MARKERS) and typed_pass:
                    raise RuntimeError("Feide reported a login error (wrong username/password?)")
                if not typed_org:
                    box = _first_visible(page, ORG_SEARCH_SELECTORS)
                    if box is not None:
                        box.click()
                        box.fill(cfg.auth.feide_org)
                        time.sleep(1.0)
                        option = _first_visible(
                            page,
                            [
                                f".selectize-dropdown-content .option:has-text('{cfg.auth.feide_org}')",
                                f"[role='option']:has-text('{cfg.auth.feide_org}')",
                                f"li:has-text('{cfg.auth.feide_org}')",
                                f"button:has-text('{cfg.auth.feide_org}')",
                                f"a:has-text('{cfg.auth.feide_org}')",
                            ],
                        )
                        if option is not None:
                            option.click()
                        else:
                            box.press("Enter")
                        typed_org = True
                        continue
                user_box = _first_visible(page, USERNAME_SELECTORS)
                pass_box = _first_visible(page, PASSWORD_SELECTORS)
                if user_box is not None and not typed_user:
                    user_box.fill(username)
                    typed_user = True
                if pass_box is not None and not typed_pass:
                    pass_box.fill(password)
                    typed_pass = True
                    submit = _first_visible(page, SUBMIT_SELECTORS)
                    if submit is not None:
                        submit.click()
                    else:
                        pass_box.press("Enter")
                    continue
                if user_box is not None and typed_user and pass_box is None:
                    submit = _first_visible(page, SUBMIT_SELECTORS)
                    if submit is not None:
                        submit.click()
                        continue
                cont = _first_visible(page, CONTINUE_SELECTORS)
                if cont is not None:
                    cont.click()
            except RuntimeError:
                _snapshot(cfg, page, "error")
                context.close()
                raise
            except Exception as exc:  # noqa: BLE001 - keep looping while pages navigate
                log.debug("login step %d: %s", step, exc)
        else:
            _snapshot(cfg, page, "timeout")
            context.close()
            raise RuntimeError(f"Automated Feide login did not finish within {timeout_s}s (see {cfg.capture_dir}/login-*.png)")
        try:
            page.goto(cfg.timeedit.reserve_url, wait_until="domcontentloaded")
            time.sleep(0.5)
        except Exception:  # noqa: BLE001
            pass
        state = context.storage_state()
        context.close()
    save_storage_state(cfg, state)
    return state


def credentials_from_env(cfg: Config) -> tuple[str, str] | None:
    username = os.environ.get(cfg.auth.username_env, "").strip()
    password = os.environ.get(cfg.auth.password_env, "")
    if username and password:
        return username, password
    return None


def ensure_session(cfg: Config, client: TimeEditClient, allow_interactive: bool = True, force: bool = False) -> bool:
    """Make sure ``client`` carries a valid session, logging in if needed."""
    if not force:
        try:
            if client.has_te_cookie() and client.is_logged_in():
                return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Session check failed: %s", exc)
    creds = credentials_from_env(cfg)
    if cfg.auth.method == "feide" or (creds and not allow_interactive):
        if not creds:
            raise RuntimeError(
                f"auth.method is 'feide' but {cfg.auth.username_env}/{cfg.auth.password_env} are not set"
            )
        log.info("Logging in to Feide automatically as %s", creds[0])
        state = automated_feide_login(cfg, *creds)
    elif allow_interactive:
        log.info("Opening a browser for interactive login")
        state = interactive_login(cfg)
    else:
        return False
    client.session.cookies.clear()
    client.set_cookies(state)
    ok = client.is_logged_in()
    log.info("Session valid: %s (cookies: %s)", ok, ", ".join(client.cookie_names()))
    return ok


def relogin_callback(cfg: Config, client: TimeEditClient, allow_interactive: bool) -> Callable[[], bool]:
    def _relogin() -> bool:
        try:
            return ensure_session(cfg, client, allow_interactive=allow_interactive, force=True)
        except Exception as exc:  # noqa: BLE001
            log.error("Re-login failed: %s", exc)
            return False

    return _relogin


def describe_session(cfg: Config) -> str:
    age = storage_age_hours(cfg)
    if age is None:
        return "no saved session"
    when = datetime.fromtimestamp(cfg.storage_state_file.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    return f"saved session from {when} ({age:.1f}h old)"
