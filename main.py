"""Skeddy - Reminder bot for Telegram.

Single-process entrypoint that runs:
  1. The python-telegram-bot application (long-polling, talks to Telegram)
  2. The APScheduler loop (fires reminders on time)
  3. A FastAPI service (serves the mini app + /api JSON endpoints)

On Render free tier the dyno sleeps after ~15 min idle. The mini app and
chat commands both run on the same web service, so any incoming request
wakes the dyno. Data lives in a hidden Telegram vault message, so it
survives redeploys and dyno restarts.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from telegram import Bot, MenuButtonWebApp, WebAppInfo
from telegram.ext import Application

from bot import handlers, miniapp_api
from bot.config import Settings, load_settings
from bot.miniapp_api import STATE
from bot.scheduler import ReminderScheduler
from bot.storage import Vault

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("skeddy")

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


async def send_reminder(user_id: int, r) -> None:
    """Send the reminder message (and optional image) to the user."""
    from bot.handlers import _fmt_next

    app = STATE.ptb_app
    if app is None:
        log.warning("PTB app not ready, skipping send for #%s", r.id)
        return

    bot: Bot = app.bot
    try:
        us = STATE.vault.db.user(user_id)
        tzname = us.timezone or STATE.settings.default_tz
        local_next = _fmt_next(r, tzname) if r.next_fire else ""
        if r.message_template:
            text = r.message_template.format(text=r.text, id=r.id, next=local_next)
        elif r.kind == "countdown":
            text = f"⏳ Countdown #{r.id}: {r.text}\nNext: {local_next}"
        else:
            text = f"Reminder #{r.id}: {r.text}\nNext: {local_next}"
        # no reply_markup: reminders are plain text/photo messages, no buttons
        image_data = await STATE.vault.get_image(r.image_id)
        if image_data is None and r.image_file_id:
            # legacy reminders created before image uploads used raw file_ids
            await bot.send_photo(user_id, r.image_file_id, caption=text)
        elif image_data is not None:
            await bot.send_photo(user_id, image_data, caption=text)
        else:
            await bot.send_message(user_id, text)
    except Exception:  # noqa: BLE001
        log.exception("Failed to send reminder #%s to user %s", r.id, user_id)
        local_next = _fmt_next(r, tzname) if r.next_fire else ""
        if r.message_template:
            text = r.message_template.format(text=r.text, id=r.id, next=local_next)
        elif r.kind == "countdown":
            text = f"⏳ Countdown #{r.id}: {r.text}\nNext: {local_next}"
        else:
            text = f"Reminder #{r.id}: {r.text}\nNext: {local_next}"
        if r.image_file_id:
            await bot.send_photo(user_id, r.image_file_id, caption=text)
        else:
            await bot.send_message(user_id, text)
    except Exception:  # noqa: BLE001
        log.exception("Failed to send reminder #%s to user %s", r.id, user_id)


def build_fastapi() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield

    app = FastAPI(title="Skeddy", version="1.0.0", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    app.include_router(miniapp_api.router)

    @app.get("/")
    async def index():
        return FileResponse(os.path.join(WEB_DIR, "index.html"))

    @app.get("/app.js")
    async def app_js():
        return FileResponse(os.path.join(WEB_DIR, "app.js"), media_type="application/javascript")

    return app


async def start_bot(settings: Settings, vault: Vault, scheduler: ReminderScheduler) -> Application:
    log.info("Building PTB application")
    ptb_app = Application.builder().token(settings.bot_token).build()
    ptb_app.bot_data["settings"] = settings
    ptb_app.bot_data["vault"] = vault
    ptb_app.bot_data["scheduler"] = scheduler
    handlers.register_handlers(ptb_app)
    try:
        await ptb_app.bot.set_my_commands(handlers.BOT_COMMANDS)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not set commands: %s", exc)
    mini_app_url = os.getenv("MINI_APP_URL", "").strip()
    if mini_app_url:
        try:
            await ptb_app.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="Mini App", web_app=WebAppInfo(url=mini_app_url)
                )
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not set menu button: %s", exc)

    async def post_init(app: Application) -> None:
        log.info("Post-init: starting scheduler")
        await scheduler.start()

    ptb_app.post_init = post_init
    log.info("Starting PTB long polling")
    await ptb_app.initialize()
    await ptb_app.start()
    await ptb_app.updater.start_polling(drop_pending_updates=True)
    await post_init(ptb_app)
    return ptb_app


async def stop_bot(ptb_app, scheduler: ReminderScheduler) -> None:
    if ptb_app is None:
        return
    try:
        await scheduler.shutdown()
    except Exception:  # noqa: BLE001
        pass
    try:
        if ptb_app.updater.running:
            await ptb_app.updater.stop()
        await ptb_app.stop()
        await ptb_app.shutdown()
    except Exception:  # noqa: BLE001
        pass


def main() -> None:
    log.info("Skeddy starting")
    settings = load_settings()
    vault = Vault(settings)
    scheduler = ReminderScheduler(settings, vault, send_reminder)

    async def run():
        await vault.setup()
        log.info("Vault ready: %d reminders, %d users", len(vault.db.reminders), len(vault.db.users))
        ptb_app = await start_bot(settings, vault, scheduler)
        STATE.settings = settings
        STATE.vault = vault
        STATE.sched = scheduler
        STATE.ptb_app = ptb_app
        fastapi_app = build_fastapi()
        config = uvicorn.Config(fastapi_app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")), log_level="info")
        server = uvicorn.Server(config)

        async def wait_stop():
            stop_event = asyncio.Event()
            def _sig():
                log.info("Shutdown signal received")
                stop_event.set()
            loop = asyncio.get_running_loop()
            for s in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(s, _sig)
                except NotImplementedError:
                    pass
            await stop_event.wait()

        stop_task = asyncio.create_task(wait_stop())
        try:
            await server.serve()
        finally:
            stop_task.cancel()
            await stop_bot(ptb_app, scheduler)
            log.info("Skeddy stopped")

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("Keyboard interrupt")


if __name__ == "__main__":
    main()