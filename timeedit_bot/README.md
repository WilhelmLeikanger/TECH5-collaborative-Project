# te_bot — TimeEdit group-room sniper for NHH

A small, precise and *polite* bot that books an NHH group room the moment a new day opens in
[TimeEdit](https://cloud.timeedit.net/nhh/web/schedule/ri1Q56.html) (every evening at 21:00, 14 days
ahead), going for your favourite room and the longest block the rules allow.

```
21:00:00.000  release instant (server clock, measured)
21:00:00.040  POST reserve  Grupperom U28 08:00-12:00  → 409 "outside the date limits"   (retry in 150 ms)
21:00:00.190  POST reserve  Grupperom U28 08:00-12:00  → 302 …&id=1234567&newres=t      ✔ booked
```

> **Status.** Everything in this folder was built and tested against a faithful mock of TimeEdit's
> reservation protocol (37 automated tests, including headless-browser tests of the login and
> learn flows, plus an end-to-end `selftest`). It has **not yet been run
> against the live NHH site** (this was written from a sandbox without access to `cloud.timeedit.net`).
> The protocol itself is well documented — the same endpoints and fields are used by independent
> open-source clients for Chalmers, LiU, HiG/NTNU — but NHH's instance has its own ids and form
> fields. The one-time `learn` step below records those from a real booking, so the first live run is
> a 5-minute supervised job. See "First live run" and "If something is off".

## How the strategy works

NHH's rules (nhh.no → For students → Group rooms):

| Rule | What the bot does |
|------|-------------------|
| Booking opens **14 days ahead**, one new day each evening (**21:00**) | Each evening it targets *today + 14*, syncs its clock to the server and fires at 21:00:00 |
| **Max 4 hours** per reservation | Tries 4-hour blocks first, then 3h, 2h, 1h |
| **Max 4 reservations per week** | Only books on the weekdays you list (default Mon–Thu) |
| Rooms released after 15 min no-show | (Show up, or cancel what you don't use) |

Attempt order (configurable): for your **#1 room**, try every preferred window with the longest block
(`08:00-12:00`, `12:00-16:00`, `09:00-13:00`, …); if the room is taken for all of them, move to room
**#2**, and so on; only then fall back to shorter blocks. Before 21:00 it reads the week grid for the
target day, so rooms already blocked by teaching or by TimeEdit itself (U21–U27 in the screenshot) are
skipped instead of wasting requests. Every rejection is classified from the server's message:

* *outside the date limits* → the day has not opened yet → retry the same slot every 150 ms
* *occupied* → next candidate immediately
* *maximum booking length* → drop all candidates that long, continue with shorter ones
* *maximum number of reservations* → stop for tonight
* *not logged in* → re-login (automatic if Feide credentials are configured), then continue

The bot never sends more than ~7 requests/second, and only during the first 45 seconds; after that it
slows to one every 2 seconds and gives up after 10 minutes.

## How it talks to TimeEdit

TimeEdit's web reservation UI ("TE Reserve") is plain form posts behind a session cookie:

| Step | Request |
|------|---------|
| CSRF token | `POST ri1Q56.html` body `CSTTG=gen` → plain integer |
| **Reserve** | `POST ri1Q56.html` body `kind=reserve&o=<room id>.<type>&dates=YYYYMMDD&starttime=08:00&endtime=12:00&aos=&url=…&fe2=…&CSTT=<token>` → **302** `Location: …?id=<reservation id>&newres=t`, or **409** + message |
| Room search | `GET objects.json?sid=12&types=<type>&…` (also filters to *free* rooms with `dates`/`starttime`/`endtime`) |
| Week grid | `GET ri.html?h=t&sid=12&objects=…&part=t&partajax=t&p=YYYYMMDD-YYYYMMDD` → HTML fragment |
| My bookings / cancel | `GET my.json`, `DELETE my.html?id=…` |

`ri1Q56` is TimeEdit's scrambled URL key; it decodes to `ri.html?h=t&sid=12` (`te_bot/scramble.py`),
so NHH's reservation search id is **12**. Authentication is Feide (SAML) → the `TEnhhweb` session
cookie; the bot gets it from a real Chromium window once and reuses it (`storage_state.json`).

## Setup (once, ~10 minutes)

Requirements: Python 3.10+, Chromium via Playwright.

```bash
cd timeedit_bot
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
python -m te_bot selftest          # exercises the whole pipeline against a built-in fake TimeEdit
python -m te_bot init              # writes ~/.timeedit-bot/config.toml — edit rooms/windows/weekdays
python -m te_bot login             # opens a browser: log in with Feide; closes itself when done
python -m te_bot discover          # finds room ids, the room type and the reservation form fields
python -m te_bot learn             # make ONE 1-hour test booking by hand; the exact request is recorded
python -m te_bot plan              # shows exactly what tonight's run will try
```

`learn` is the important one: it watches the browser make a reservation and stores the request as
`template.json` (which `o` objects, which `fe*` fields, the locale, the URL). From then on the bot
only swaps room, date, times and CSRF token, so it sends *exactly* what the website sends. Cancel the
test booking afterwards (in TimeEdit, or `python -m te_bot cancel <id>`).

### First live run

Do the first run supervised, with a dry run first:

```bash
python -m te_bot run --now --dry-run --date 2026-11-15     # everything except the final POST
python -m te_bot run --now --date 2026-11-15               # real booking of an already-open day
python -m te_bot run --once                                 # tonight at 21:00, then exit
```

Watch the log lines: every attempt prints the room, the outcome class and the server's message.

## Every evening

```bash
python -m te_bot run        # daemon: sleeps until 20:50, books at 21:00, repeats daily
```

Or schedule `run --once --no-interactive` at 20:50 with systemd/launchd/Task Scheduler — ready-made
files and the commands are in [`deploy/`](deploy/README.md). The machine must be awake at 20:50;
sessions expire eventually, so either run `login` again when `check` says so, or set
`[auth] method = "feide"` with `FEIDE_USERNAME`/`FEIDE_PASSWORD` in the environment for unattended
re-login.

## Commands

| Command | Purpose |
|---------|---------|
| `init` | write `config.toml` from the template |
| `login [--auto]` | log in via browser (or automated Feide with env credentials) and save the session |
| `check` | session validity, your current reservations, next release time |
| `discover` | detect rooms/ids/form fields; writes `discovered.json` and raw HTML to `captures/` |
| `learn` | record one manual booking → `template.json` (+ a HAR of all traffic for debugging) |
| `plan [--date D]` | print the attempt list for a day, with quota and availability |
| `run [--once] [--now] [--date D] [--dry-run] [--no-interactive]` | wait for the release and book |
| `status` | bookings made by the bot, recent runs |
| `cancel <id>` | cancel a reservation |
| `selftest` / `mock` | run against the built-in fake TimeEdit |

All data lives in `~/.timeedit-bot/` (`TE_BOT_HOME` to change): `config.toml`, `storage_state.json`
(cookies — keep private), `template.json`, `discovered.json`, `state.json`, `logs/`, `captures/`.

## Configuration

See [`config.example.toml`](config.example.toml) — every key is documented there. The ones you will
actually touch:

```toml
[strategy]
rooms    = ["Grupperom U28", "Grupperom U30", "Grupperom U29"]   # priority order ("U28" works too)
windows  = ["08:00-12:00", "12:00-16:00", "09:00-13:00", "13:00-17:00"]
weekdays = ["mon", "tue", "wed", "thu"]                          # 4 per week is the cap
order    = "room-first"                                          # or "window-first"
```

## If something is off

* **`login` never finishes** – the page must show "My reservations"; if NHH's page uses other words,
  add them to `_LOGGED_IN_MARKERS` in `te_bot/timeedit.py`.
* **`discover` finds no rooms** – look at `~/.timeedit-bot/captures/reserve_page.html`; set
  `[timeedit] room_type` by hand if the guess is wrong, or just run `learn` (its template contains the
  room object and type).
* **Every attempt is "unknown"** – read the server message in the log; most likely a required form
  field. `learn` fixes that by copying the real request; or set `[strategy] fields = { fe2 = "…" }`.
* **"not open yet" never turns into success** – the release may not be exactly 21:00 server time:
  check `logs/run-<date>.json` for the timestamp of the first success on a night it worked, and adjust
  `[release] time`, `lead_ms`, `burst_seconds`.
* **Session keeps expiring** – use `[auth] method = "feide"` (automated login) or run `login` in the
  afternoon. `check` tells you the state.

Logs: `~/.timeedit-bot/logs/te_bot.log` (everything) and `run-<date>.json` (one record per attempt).

## Tests

```bash
python -m pytest timeedit_bot          # from the repository root (37 tests; ~80 s, or ~5 s without the browser tests)
python -m te_bot selftest              # full pipeline incl. timing against the mock
```

`te_bot/mock_server.py` is a faithful fake of the protocol (CSRF, 302/409 outcomes, release gating,
week grid, `objects.json`, `my.json`, cancel) — handy for experimenting without touching the real site.

## Fair use

This books rooms on *your own* account, at human-like request rates, within NHH's rules (4 h, 4 per
week). Don't lower `min_request_gap_ms`, don't run it for other people's accounts, and cancel rooms
you won't use so others can have them.

## The brief this implements (refined from the original prompt)

> Build a bot that books an NHH group room through TimeEdit (`cloud.timeedit.net/nhh/web/schedule/ri1Q56.html`)
> at exactly 21:00 Europe/Oslo every evening, when the day 14 days ahead opens. Strategy: a prioritized
> list of rooms (e.g. U28, U30, U29 …) and time windows; for the top room try the longest block allowed
> by NHH's rules (4 h) in each preferred window, fall back to the next room, then to shorter blocks.
> Respect the 4-reservations-per-week limit by booking only chosen weekdays. Fire from a clock synced
> to the server, retry quickly while the server says the day is not open yet, classify every rejection
> (occupied / too long / quota / session expired) and act accordingly, and log each attempt. Log in
> through Feide in a real browser once and reuse the session; re-login automatically if credentials
> are provided. Learn NHH's exact reservation form from one manual booking so no field is guessed.
> Must be runnable unattended (scheduled at 20:50), be gentle to the server, and come with a mock
> server plus tests so it can be verified without hammering the live site.
