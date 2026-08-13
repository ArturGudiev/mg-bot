"""
Telegram support relay bot.

Users write to the bot in private chat → message is posted to ADMIN_CHAT_ID.
Reply to that message in the admin chat → user receives the reply from the bot.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("telegram_bot")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "").strip()

# Survives process restarts: user id is embedded in the admin-chat message.
USER_MARKER = re.compile(r"^#u(\d+)\n")


def _require_env() -> tuple[str, int]:
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    if not ADMIN_CHAT_ID:
        raise SystemExit("TELEGRAM_ADMIN_CHAT_ID is not set")
    try:
        admin_id = int(ADMIN_CHAT_ID)
    except ValueError as exc:
        raise SystemExit("TELEGRAM_ADMIN_CHAT_ID must be an integer") from exc
    return BOT_TOKEN, admin_id


def _user_header(user) -> str:
    username = f"@{user.username}" if user.username else "—"
    name = user.full_name or "Unknown"
    return f"#u{user.id}\n{name} ({username})\n\n"


async def from_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Forward a private user message to the admin chat."""
    if not update.message or not update.effective_user:
        return

    admin_chat_id: int = context.application.bot_data["admin_chat_id"]
    header = _user_header(update.effective_user)
    msg = update.message

    if msg.text:
        await context.bot.send_message(admin_chat_id, header + msg.text)
        return

    # Media: send a marker text, then copy the media so admins can reply to the marker.
    marker = await context.bot.send_message(
        admin_chat_id,
        header + (msg.caption or "[media]"),
    )
    try:
        await msg.copy(chat_id=admin_chat_id, reply_to_message_id=marker.message_id)
    except Exception:
        logger.exception("failed to copy media to admin chat")


async def from_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """If an admin replies to a relayed message, send that reply to the user."""
    if not update.message or not update.message.reply_to_message:
        return

    admin_chat_id: int = context.application.bot_data["admin_chat_id"]
    if update.effective_chat is None or update.effective_chat.id != admin_chat_id:
        return

    # Ignore the bot's own messages / non-replies already filtered.
    replied = update.message.reply_to_message
    source = replied.text or replied.caption or ""
    match = USER_MARKER.match(source)
    if not match:
        # Maybe they replied to the copied media; walk up to the marker parent.
        if replied.reply_to_message:
            source = replied.reply_to_message.text or replied.reply_to_message.caption or ""
            match = USER_MARKER.match(source)
        if not match:
            logger.info("admin reply without user marker, ignored")
            return

    user_id = int(match.group(1))
    msg = update.message

    if msg.text:
        await context.bot.send_message(user_id, msg.text)
        return

    try:
        await msg.copy(chat_id=user_id)
    except Exception:
        logger.exception("failed to copy admin media to user %s", user_id)
        if msg.caption:
            await context.bot.send_message(user_id, msg.caption)


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


def _start_health_server() -> None:
    """Render Web Service expects a process listening on $PORT."""
    raw = os.environ.get("PORT", "").strip()
    if not raw:
        return
    port = int(raw)
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("health server on :%s", port)


def main() -> None:
    token, admin_chat_id = _require_env()
    _start_health_server()

    app = Application.builder().token(token).build()
    app.bot_data["admin_chat_id"] = admin_chat_id

    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, from_user)
    )
    app.add_handler(
        MessageHandler(
            filters.Chat(chat_id=admin_chat_id) & filters.REPLY & ~filters.StatusUpdate.ALL,
            from_admin,
        )
    )

    logger.info("bot starting (admin_chat_id=%s)", admin_chat_id)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
