"""Flexible Reminder - Reminder bot for Telegram.

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


async def _send_media_and_cache(bot: Bot, user_id: int, r, text: str, image_data: bytes | None) -> None:
    """Send photo/document, capturing the Telegram file_id to reuse on later sends."""
    if r.media_type == "document":
        if r.main_file_id:
            try:
                await bot.send_document(user_id, r.main_file_id, caption=text)
                return
            except Exception:
                log.warning("send_document via cached file_id failed; retrying with bytes")
        msg = await bot.send_document(user_id, image_data, filename=r.filename or "file", caption=text)
    else:
        if r.main_file_id:
            try:
                await bot.send_photo(user_id, r.main_file_id, caption=text)
                return
            except Exception:
                log.warning("send_photo via cached file_id failed; retrying with bytes")
        msg = await bot.send_photo(user_id, image_data, caption=text)
    # Capture the file_id so future sends reuse Telegram's cached file (~zero egress)
    try:
        if r.media_type == "document":
            r.main_file_id = msg.document.file_id
        else:
            r.main_file_id = msg.photo[-1].file_id
        await STATE.vault.save()
    except Exception:  # noqa: BLE001
        pass


async def send_reminder(user_id: int, r) -> None:
    """Send the reminder message (and optional media) to the user."""
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
        else:
            text = r.text
        image_data = await STATE.vault.get_image(r.image_id)
        # Prefer the cached media file_id when we have it (so the image resends
        # on every interval even if a fresh vault download fails), then fall back
        # to freshly downloaded bytes, then a legacy image_file_id.
        if r.main_file_id or image_data is not None:
            await _send_media_and_cache(bot, user_id, r, text, image_data)
        elif r.image_file_id:
            # legacy reminders that already hold a Telegram file_id
            await bot.send_photo(user_id, r.image_file_id, caption=text)
        else:
            await bot.send_message(user_id, text)
    except Exception:  # noqa: BLE001
        log.exception("Failed to send reminder #%s to user %s", r.id, user_id)
        # best-effort fallback
        try:
            local_next = _fmt_next(r, tzname) if r.next_fire else ""
            if r.message_template:
                text = r.message_template.format(text=r.text, id=r.id, next=local_next)
            else:
                text = r.text
            if r.image_file_id:
                await bot.send_photo(user_id, r.image_file_id, caption=text)
            elif r.main_file_id and r.media_type == "document":
                await bot.send_document(user_id, r.main_file_id, caption=text)
            elif r.main_file_id:
                await bot.send_photo(user_id, r.main_file_id, caption=text)
            elif r.image_id:
                image_data = await STATE.vault.get_image(r.image_id)
                if image_data and r.media_type == "document":
                    await bot.send_document(user_id, image_data, filename=r.filename or "file", caption=text)
                elif image_data:
                    await bot.send_photo(user_id, image_data, caption=text)
                else:
                    await bot.send_message(user_id, text)
            else:
                await bot.send_message(user_id, text)
        except Exception:  # noqa: BLE001
            log.exception("Failed to send reminder #%s (fallback)", r.id)


def build_fastapi() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield

    app = FastAPI(title="Flexible Reminder", version="1.0.0", lifespan=lifespan)
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
    log.info("Flexible Reminder starting")
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
            log.info("Flexible Reminder stopped")

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("Keyboard interrupt")


if __name__ == "__main__":
    main()