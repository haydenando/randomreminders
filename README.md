# Flexible Reminder - Reminder bot for Telegram

A personal Telegram reminder bot with a Flexible Reminder mini app and a killer feature:
**randomized interval reminders** ("tell me every 10-20 minutes").

- No database. All state lives in a hidden Telegram message (the *vault*), so it
  survives Render redeploys, dyno sleeps, and bot restarts.
- Free to host on [Render](https://render.com) (free web service tier).
- Python + `python-telegram-bot` v21 + `APScheduler` + `FastAPI`.

## Features

- **Secret-phrase gated**: public bot, but only works if you type the phrase
  (`haydenando` by default). After that you get a Flexible Reminder onboarding:
  a choice between "Open Mini App" and "Set up in chat" (time zone).
- **Randomized intervals**: `every 10-20m stretch` fires indefinitely every 10 to 20 minutes,
  re-rolling the random gap each time.
- **Schedule categories**: One time, Interval, and Complex (every day, days of
  the week, days of the month, date range). Multiple schedules can be attached
  to the same reminder.
- **Two reminder modes**: Regular and Countdown (count down to a target date/time,
  optionally starting N days in advance).
- **Customizable message text** per reminder (`{text}`, `{id}`, `{next}` placeholders).
- **Optional image** per reminder, uploaded from the mini app (no links/file
  ids). Bytes are cached in memory and backed up in the vault chat by the
  vault bot, so images survive restarts.
- **Buttonless reminders**: reminders arrive as plain text (or photo + caption)
  with no inline buttons. Done / delete / snooze live in the mini app.
- **Chat commands** + a **mini app** (Flexible Reminder) for full editing.

## Commands

| Command | Description |
|---|---|
| `/start` | start using the bot or return to the main menu |
| `/list` | show active reminders |
| `/delete` | delete a reminder (`/delete <id>`) |
| `/help` | open Help |
| `/formats` | view supported date and time formats |
| `/help_reminder_types` | the difference between reminders and countdowns |
| `/help_schedules` | what the icons next to a reminder's times mean |
| `/cancel` | cancel the current action |
| `/miniapp` | open the app to create/manage reminders |
| `/settings` | open settings (timezone, snooze) |

## Schedule formats

- Durations: `45s`, `10m`, `2h`, `3d`, `2w`
- Randomized intervals: `every 10-20m`
- Every N days: `every 4d at 18:00`, `every 4d at random`, `every 4d at random 08:00-20:00`
- Weekly: `every monday at 09:00`, `every weekday at 09:00`
- Cron: `cron 0 9 * * *`
- One-off: `in 2h`, `tomorrow at 09:00`, `2026-05-01 14:00`
- Combine one-offs with `~`: `in 2h ~ tomorrow at 09:00`

## Chat syntax for quick creation

```
every 10-20m stretch           # randomized interval
every 30m water               # fixed interval
every 4d at 18:00 stretch      # every 4 days at 6pm
every 2 weeks at 09:00 report  # every 2 weeks at 9am
every weekday at 09:00 standup # weekdays at 9am
cron 0 9 * * * daily           # 5-field cron
in 2h call mom                # once in 2h
tomorrow at 09:00 dentist      # once tomorrow
```

## Setup (one-time)

### 1. Create the main bot

1. Message @BotFather on Telegram.
2. Send `/newbot`, pick a username (e.g. `FlexibleReminderBot`) and a name.
3. Copy the token — this is `BOT_TOKEN`.

### 2. Create the vault bot

The vault bot is a second bot that owns the hidden data message. It keeps your
reminders alive across redeploys.

1. In @BotFather, send `/newbot` again and create a second bot (e.g. `FlexibleVaultBot`).
2. Copy its token — this is `VAULT_TOKEN`.
3. Start a chat with the vault bot (send it any message, e.g. `hi`).
4. Get the chat id: open
   `https://api.telegram.org/bot<VAULT_TOKEN>/getUpdates`
   and read the `"chat":{"id": <NUMBER>}` field. That number is `VAULT_CHAT_ID`.

> The vault message is **pinned** in that chat. If you delete it, the bot will
> just create a fresh one (and you'll start with zero reminders).

### 3. Local run (optional)

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
# .venv/bin/activate         # Linux/macOS
pip install -r requirements.txt
copy .env.example .env           # then fill in the values
python main.py
```

### 4. Deploy on Render (free)

1. Push this folder to GitHub (already set up as `haydenando/randomreminders`).
2. In the [Render dashboard](https://dashboard.render.com), click **New +** → **Web Service**.
3. Connect the `randomreminders` repo.
4. Plan: **Free**. Build command: `pip install -r requirements.txt`.
   Start command: `python main.py`.
5. Add environment variables: `BOT_TOKEN`, `VAULT_TOKEN`, `VAULT_CHAT_ID`,
   `SECRET_PHRASE`, `DEFAULT_TIMEZONE`, and after the first deploy, set
   `MINI_APP_URL` to your Render URL (e.g. `https://<name>.onrender.com`).
6. Deploy. Once it's live, set `MINI_APP_URL` and redeploy.

## How the vault works

On first start, the bot (using the vault bot's token) sends a JSON blob to the
vault chat and **pins** it. Every change to reminders or user settings edits
that same message. On the next boot, the bot reads the pinned message to
restore state. Because the message lives in Telegram's backend — not on
Render's disk — your data survives every redeploy and dyno restart.

Images work the same way: when you upload one in the mini app, the bytes are
cached in memory and sent to the vault chat as a document by the vault bot.
The vault message stores the vault bot's `file_id` for it, so after a restart
the image can be re-downloaded and sent by the main bot.

## Project layout

```
main.py            # entrypoint: PTB + APScheduler + FastAPI in one process
bot/
  config.py        # settings + Reminder dataclass
  storage.py       # vault (hidden Telegram message) persistence
  scheduler.py     # APScheduler wrapper + next-fire computation
  handlers.py      # chat command + callback handlers
  miniapp_api.py   # FastAPI router for the web app
web/
  index.html       # Flexible Reminder mini app UI
  app.js           # mini app logic
requirements.txt
.env.example
render.yaml
```

## Notes & limits

- Free Render dynos sleep after ~15 min idle; the next message wakes it (a few
  seconds of latency on first contact).
- The mini app is opened in-app via the `/miniapp` button (a normal web link).
  For the native mini-app chrome you would register the URL with BotFather;
  the link works either way.
- Snooze works for `task` reminders. `reminder` types auto-complete when sent.
