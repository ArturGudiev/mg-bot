"""
Telegram support relay bot (Render Free Web Service compatible).

Users write to the bot in private chat → message is posted to ADMIN_CHAT_ID.
Reply to that message in the admin chat → user receives the reply from the bot.

Long polling + aiohttp fake web server on $PORT (required by Render Free).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatType
from aiogram.types import Message
from aiohttp import web

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("telegram_bot")

# Survives process restarts: user id is embedded in the admin-chat message.
USER_MARKER = re.compile(r"^#u(\d+)\n")


def require_env() -> tuple[str, int]:
    token = (
        os.getenv("TELEGRAM_BOT_TOKEN")
        or os.getenv("TELEGRAM_TOKEN")
        or ""
    ).strip()
    admin_raw = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "").strip()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN (or TELEGRAM_TOKEN) is not set")
    if not admin_raw:
        raise SystemExit("TELEGRAM_ADMIN_CHAT_ID is not set")
    try:
        admin_chat_id = int(admin_raw)
    except ValueError as exc:
        raise SystemExit("TELEGRAM_ADMIN_CHAT_ID must be an integer") from exc
    return token, admin_chat_id


def user_header(user) -> str:
    username = f"@{user.username}" if user.username else "—"
    name = user.full_name or "Unknown"
    return f"#u{user.id}\n{name} ({username})\n\n"


def extract_user_id(source: str | None) -> int | None:
    if not source:
        return None
    match = USER_MARKER.match(source)
    return int(match.group(1)) if match else None


async def handle_ping(_request: web.Request) -> web.Response:
    return web.Response(text="Bot is running")


async def start_web_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("fake web server on :%s (for Render Free Web Service)", port)
    return runner


async def main() -> None:
    token, admin_chat_id = require_env()
    bot = Bot(token=token)
    dp = Dispatcher()

    await start_web_server()

    @dp.message(F.chat.type == ChatType.PRIVATE)
    async def from_user(message: Message) -> None:
        if not message.from_user:
            return
        header = user_header(message.from_user)

        if message.text:
            await bot.send_message(admin_chat_id, header + message.text)
            return

        marker = await bot.send_message(
            admin_chat_id,
            header + (message.caption or "[media]"),
        )
        try:
            await message.send_copy(
                chat_id=admin_chat_id,
                reply_to_message_id=marker.message_id,
            )
        except Exception:
            logger.exception("failed to copy media to admin chat")

    @dp.message(F.chat.id == admin_chat_id, F.reply_to_message)
    async def from_admin(message: Message) -> None:
        replied = message.reply_to_message
        if not replied:
            return

        user_id = extract_user_id(replied.text or replied.caption)
        if user_id is None and replied.reply_to_message:
            parent = replied.reply_to_message
            user_id = extract_user_id(parent.text or parent.caption)
        if user_id is None:
            logger.info("admin reply without user marker, ignored")
            return

        if message.text:
            await bot.send_message(user_id, message.text)
            return

        try:
            await message.send_copy(chat_id=user_id)
        except Exception:
            logger.exception("failed to copy admin media to user %s", user_id)
            if message.caption:
                await bot.send_message(user_id, message.caption)

    logger.info("bot starting with long polling (admin_chat_id=%s)", admin_chat_id)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
