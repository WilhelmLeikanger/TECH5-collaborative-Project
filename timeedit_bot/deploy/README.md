# Running te_bot every evening

The bot needs to be *running* at 20:50–21:01 every evening. Three ways:

## 1. Simplest: leave a terminal open (daemon mode)

```bash
python -m te_bot run
```

It sleeps until 20:50, books at 21:00, prints the result and sleeps until the next evening.
Keep the laptop plugged in and stop it from sleeping (macOS: `caffeinate -i python -m te_bot run`;
Windows: Settings → Power → never sleep when plugged in).

## 2. Scheduled task, one release per run (`run --once`)

| OS | How |
|----|-----|
| Linux (systemd) | `cp deploy/te-bot.* ~/.config/systemd/user/`, edit the paths, `systemctl --user enable --now te-bot.timer`, `loginctl enable-linger $USER` |
| macOS (launchd) | `deploy/com.te-bot.plist` → `~/Library/LaunchAgents/`, replace `REPLACE_ME`, `launchctl load …` |
| Windows (Task Scheduler) | see below |
| cron | `50 20 * * * cd /path/to/timeedit_bot && .venv/bin/python -m te_bot run --once --no-interactive >> ~/.timeedit-bot/logs/cron.log 2>&1` |

Windows, from an elevated PowerShell (adjust paths):

```powershell
$action = New-ScheduledTaskAction -Execute "C:\path\to\timeedit_bot\.venv\Scripts\python.exe" -Argument "-m te_bot run --once --no-interactive" -WorkingDirectory "C:\path\to\timeedit_bot"
$trigger = New-ScheduledTaskTrigger -Daily -At 20:50
$settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName "te_bot" -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest
```

`-WakeToRun` wakes the PC from sleep; keep it plugged in.

## 3. Always-on box (Raspberry Pi / VPS)

Run `python -m te_bot login` on your laptop first (Feide needs a real browser), then copy
`~/.timeedit-bot/` (config, `storage_state.json`, `discovered.json`, `template.json`) to the box and
use option 1 or 2 there. Sessions expire eventually; for a fully unattended box set
`[auth] method = "feide"` with `FEIDE_USERNAME`/`FEIDE_PASSWORD` in the environment so the bot can
log in headlessly (`playwright install chromium --with-deps` on the box).

## Scheduled runs never open a browser

`--no-interactive` means: if the saved session is invalid and no Feide credentials are configured,
the run logs an error and sends a desktop notification instead of waiting for you. Check
`python -m te_bot check` in the afternoon if you want to be sure.
