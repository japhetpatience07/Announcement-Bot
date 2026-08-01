"""
Announcement Bot - Telegram bot for broadcasting announcements to subscribers,
groups, and channels. Uses only the Telegram Bot API (no external services).

Features:
- /subscribe, /unsubscribe   - users opt in/out of DMs from the bot
- Auto-tracks groups/channels the bot is added to (as long as it has permission
  to post there)
- /broadcast (admin-only)    - guided flow to compose and send an announcement:
    1. Send the content (text, photo, video, or document w/ caption)
    2. Optionally add inline buttons ("Button Text - https://url.com" per line)
    3. Choose targets: Subscribers / Groups / Channels / any combination
    4. Send now, or schedule for a later date/time
- /stats (admin-only)        - subscriber/group/channel counts + broadcasts sent
- /cancel                    - cancel an in-progress /broadcast flow

Setup:
1. pip install python-telegram-bot[job-queue] --upgrade
2. Create a bot via @BotFather, get your token
3. Set BOT_TOKEN and ADMIN_IDS as environment variables (see below)
4. Add the bot to your groups/channels as ADMIN (needs "Post Messages" rights
   in channels, and should be an admin in groups for reliability)
5. Run: python announcement_bot.py

Environment variables:
- BOT_TOKEN   (required) - your bot token from BotFather
- ADMIN_IDS   (required) - comma-separated Telegram user IDs allowed to run
                            /broadcast and /stats, e.g. "111111,222222"
- DB_PATH     (optional) - path to the SQLite file, defaults to "announce.db"

Scheduling notes:
- Scheduled announcements are stored in SQLite, so they survive bot restarts.
- On startup, any announcement whose scheduled time already passed while the
  bot was offline is sent immediately (catch-up); future ones are re-armed.
- Schedule times are interpreted as UTC. Enter them as "YYYY-MM-DD HH:MM".
"""

import io
import json
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "PASTE_YOUR_BOT_TOKEN_HERE")
ADMIN_IDS = {
    int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x
}
DB_PATH = os.environ.get("DB_PATH", "announce.db")

# Conversation states
CONTENT, BUTTONS, TARGETS, SCHEDULE = range(4)


# ------------------------------------------------------------------
# DATABASE
# ------------------------------------------------------------------

def init_db() -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS subscribers (
                user_id INTEGER PRIMARY KEY,
                subscribed_at TEXT DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY,
                chat_type TEXT NOT NULL,
                title TEXT,
                added_at TEXT DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS scheduled (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_chat_id INTEGER NOT NULL,
                source_message_id INTEGER NOT NULL,
                buttons TEXT,
                targets TEXT NOT NULL,
                send_at TEXT NOT NULL,
                sent INTEGER NOT NULL DEFAULT 0,
                created_by INTEGER
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS broadcast_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sent_at TEXT DEFAULT CURRENT_TIMESTAMP,
                targets TEXT,
                recipients INTEGER
            )"""
        )
        conn.commit()


def db_execute(query: str, params: tuple = ()) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(query, params)
        conn.commit()


def db_query(query: str, params: tuple = ()) -> list:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(query, params)
        return cur.fetchall()


def add_subscriber(user_id: int) -> bool:
    """Returns True if newly added, False if already subscribed."""
    existing = db_query("SELECT 1 FROM subscribers WHERE user_id = ?", (user_id,))
    if existing:
        return False
    db_execute("INSERT INTO subscribers (user_id) VALUES (?)", (user_id,))
    return True


def remove_subscriber(user_id: int) -> bool:
    existing = db_query("SELECT 1 FROM subscribers WHERE user_id = ?", (user_id,))
    if not existing:
        return False
    db_execute("DELETE FROM subscribers WHERE user_id = ?", (user_id,))
    return True


def upsert_chat(chat_id: int, chat_type: str, title: Optional[str]) -> None:
    db_execute(
        "INSERT OR REPLACE INTO chats (chat_id, chat_type, title) VALUES (?, ?, ?)",
        (chat_id, chat_type, title),
    )


def remove_chat(chat_id: int) -> None:
    db_execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))


def get_chats(chat_type: str) -> list[int]:
    rows = db_query("SELECT chat_id FROM chats WHERE chat_type = ?", (chat_type,))
    return [r[0] for r in rows]


def get_subscribers() -> list[int]:
    rows = db_query("SELECT user_id FROM subscribers")
    return [r[0] for r in rows]


# ------------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------------

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def parse_buttons(text: str) -> Optional[list[list[InlineKeyboardButton]]]:
    """Parses lines like 'Label - https://url.com' into an inline keyboard."""
    text = text.strip()
    if not text or text.lower() == "skip":
        return None
    rows = []
    for line in text.splitlines():
        if "-" not in line:
            continue
        label, _, url = line.partition("-")
        label, url = label.strip(), url.strip()
        if label and url.startswith(("http://", "https://", "tg://")):
            rows.append([InlineKeyboardButton(label, url=url)])
    return rows or None


def buttons_to_json(markup: Optional[list[list[InlineKeyboardButton]]]) -> Optional[str]:
    if not markup:
        return None
    return json.dumps(
        [[{"text": b.text, "url": b.url} for b in row] for row in markup]
    )


def buttons_from_json(data: Optional[str]) -> Optional[InlineKeyboardMarkup]:
    if not data:
        return None
    rows = json.loads(data)
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(b["text"], url=b["url"]) for b in row] for row in rows]
    )


async def resolve_targets(target_keys: list[str]) -> list[int]:
    chat_ids: list[int] = []
    if "subscribers" in target_keys:
        chat_ids += get_subscribers()
    if "groups" in target_keys:
        chat_ids += get_chats("group")
    if "channels" in target_keys:
        chat_ids += get_chats("channel")
    return list(dict.fromkeys(chat_ids))  # de-duplicate, preserve order


async def do_broadcast(
    context: ContextTypes.DEFAULT_TYPE,
    source_chat_id: int,
    source_message_id: int,
    target_keys: list[str],
    reply_markup: Optional[InlineKeyboardMarkup],
) -> int:
    chat_ids = await resolve_targets(target_keys)
    sent = 0
    for chat_id in chat_ids:
        try:
            await context.bot.copy_message(
                chat_id=chat_id,
                from_chat_id=source_chat_id,
                message_id=source_message_id,
                reply_markup=reply_markup,
            )
            sent += 1
        except Exception as e:
            logger.warning(f"Failed to send to {chat_id}: {e}")
    db_execute(
        "INSERT INTO broadcast_log (targets, recipients) VALUES (?, ?)",
        (json.dumps(target_keys), sent),
    )
    return sent


# ------------------------------------------------------------------
# BASIC COMMANDS
# ------------------------------------------------------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📢 Announcement Bot\n\n"
        "/subscribe - get announcements here\n"
        "/unsubscribe - stop getting them\n"
        "/myid - get your Telegram user ID\n"
        + ("\nAdmin: /broadcast, /stats" if is_admin(update.effective_user.id) else "")
    )


async def myid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.message.reply_text(
        f"🆔 Your Telegram user ID is: `{user.id}`\n\n"
        "Send this to whoever manages the bot if they need to add you as an admin.",
        parse_mode="Markdown",
    )


async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Please message me privately to subscribe.")
        return
    added = add_subscriber(update.effective_user.id)
    await update.message.reply_text(
        "✅ Subscribed! You'll receive announcements here." if added
        else "You're already subscribed."
    )


async def unsubscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    removed = remove_subscriber(update.effective_user.id)
    await update.message.reply_text(
        "❌ Unsubscribed. You won't receive announcements anymore." if removed
        else "You weren't subscribed."
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("This command is admin-only.")
        return
    subs = len(get_subscribers())
    groups = len(get_chats("group"))
    channels = len(get_chats("channel"))
    total_broadcasts = db_query("SELECT COUNT(*), COALESCE(SUM(recipients),0) FROM broadcast_log")[0]
    pending = db_query("SELECT COUNT(*) FROM scheduled WHERE sent = 0")[0][0]
    await update.message.reply_text(
        "📊 *Stats*\n"
        f"Subscribers: {subs}\n"
        f"Groups: {groups}\n"
        f"Channels: {channels}\n"
        f"Broadcasts sent: {total_broadcasts[0]} (total recipients: {total_broadcasts[1]})\n"
        f"Pending scheduled: {pending}",
        parse_mode="Markdown",
    )


# ------------------------------------------------------------------
# CHAT TRACKING (bot added/removed from groups & channels)
# ------------------------------------------------------------------

async def on_bot_membership_change(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    result = update.my_chat_member
    if result is None:
        return
    chat = result.chat
    new_status = result.new_chat_member.status

    if new_status in ("member", "administrator"):
        chat_type = "channel" if chat.type == ChatType.CHANNEL else "group"
        upsert_chat(chat.id, chat_type, chat.title)
        logger.info(f"Added to {chat_type}: {chat.title} ({chat.id})")
    elif new_status in ("left", "kicked"):
        remove_chat(chat.id)
        logger.info(f"Removed from chat: {chat.title} ({chat.id})")


# ------------------------------------------------------------------
# /broadcast CONVERSATION
# ------------------------------------------------------------------

async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("This command is admin-only.")
        return ConversationHandler.END
    context.user_data.clear()
    await update.message.reply_text(
        "📝 Send me the announcement content now.\n"
        "Text, photo, video, or document (with caption) all work.\n\n"
        "/cancel to abort."
    )
    return CONTENT


async def broadcast_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["source_chat_id"] = update.effective_chat.id
    context.user_data["source_message_id"] = update.message.message_id
    await update.message.reply_text(
        "🔘 Add inline buttons? Send one per line as:\n"
        "`Button Text - https://example.com`\n\n"
        "Or type `skip` for no buttons.",
        parse_mode="Markdown",
    )
    return BUTTONS


async def broadcast_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    markup_rows = parse_buttons(update.message.text or "")
    context.user_data["buttons_json"] = buttons_to_json(markup_rows)

    keyboard = [
        [InlineKeyboardButton("👤 Subscribers", callback_data="toggle_subscribers")],
        [InlineKeyboardButton("👥 Groups", callback_data="toggle_groups")],
        [InlineKeyboardButton("📣 Channels", callback_data="toggle_channels")],
        [InlineKeyboardButton("✅ Done choosing", callback_data="targets_done")],
    ]
    context.user_data["targets"] = set()
    await update.message.reply_text(
        "🎯 Choose targets (tap to toggle, then 'Done choosing'):",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return TARGETS


async def broadcast_targets_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    targets: set = context.user_data.setdefault("targets", set())

    if query.data == "targets_done":
        if not targets:
            await query.answer("Pick at least one target first.", show_alert=True)
            return TARGETS
        await query.edit_message_text(
            f"Targets selected: {', '.join(sorted(targets))}\n\n"
            "⏰ Send `now` to broadcast immediately, or a UTC date/time to "
            "schedule it, formatted as `YYYY-MM-DD HH:MM`.",
            parse_mode="Markdown",
        )
        return SCHEDULE

    key = query.data.replace("toggle_", "")
    if key in targets:
        targets.remove(key)
    else:
        targets.add(key)

    labels = {"subscribers": "👤 Subscribers", "groups": "👥 Groups", "channels": "📣 Channels"}
    keyboard = [
        [InlineKeyboardButton(
            f"{'✅ ' if k in targets else ''}{labels[k]}", callback_data=f"toggle_{k}"
        )]
        for k in ("subscribers", "groups", "channels")
    ]
    keyboard.append([InlineKeyboardButton("✅ Done choosing", callback_data="targets_done")])
    await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
    return TARGETS


async def broadcast_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    targets = list(context.user_data.get("targets", []))
    source_chat_id = context.user_data["source_chat_id"]
    source_message_id = context.user_data["source_message_id"]
    buttons_json = context.user_data.get("buttons_json")
    reply_markup = buttons_from_json(buttons_json)

    if text.lower() == "now":
        await update.message.reply_text("📤 Sending now...")
        sent = await do_broadcast(context, source_chat_id, source_message_id, targets, reply_markup)
        await update.message.reply_text(f"✅ Sent to {sent} recipient(s).")
        context.user_data.clear()
        return ConversationHandler.END

    try:
        send_at = datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        await update.message.reply_text(
            "Couldn't parse that. Use `now` or `YYYY-MM-DD HH:MM` (UTC).",
            parse_mode="Markdown",
        )
        return SCHEDULE

    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            """INSERT INTO scheduled
               (source_chat_id, source_message_id, buttons, targets, send_at, created_by)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                source_chat_id,
                source_message_id,
                buttons_json,
                json.dumps(targets),
                send_at.isoformat(),
                update.effective_user.id,
            ),
        )
        conn.commit()
        schedule_id = cur.lastrowid

    delay = (send_at - datetime.now(timezone.utc)).total_seconds()
    context.application.job_queue.run_once(
        run_scheduled_job, when=max(delay, 0), data={"schedule_id": schedule_id}, name=f"sched_{schedule_id}"
    )

    await update.message.reply_text(f"⏰ Scheduled for {send_at.isoformat()} UTC.")
    context.user_data.clear()
    return ConversationHandler.END


async def broadcast_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ------------------------------------------------------------------
# SCHEDULED JOB EXECUTION
# ------------------------------------------------------------------

async def run_scheduled_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    schedule_id = context.job.data["schedule_id"]
    row = db_query(
        "SELECT source_chat_id, source_message_id, buttons, targets, sent FROM scheduled WHERE id = ?",
        (schedule_id,),
    )
    if not row:
        return
    source_chat_id, source_message_id, buttons_json, targets_json, sent_flag = row[0]
    if sent_flag:
        return

    targets = json.loads(targets_json)
    reply_markup = buttons_from_json(buttons_json)
    sent = await do_broadcast(context, source_chat_id, source_message_id, targets, reply_markup)
    db_execute("UPDATE scheduled SET sent = 1 WHERE id = ?", (schedule_id,))
    logger.info(f"Scheduled announcement {schedule_id} sent to {sent} recipient(s).")


async def rearm_pending_jobs(application: Application) -> None:
    """Called on startup to reschedule any pending announcements after a restart."""
    now = datetime.now(timezone.utc)
    rows = db_query("SELECT id, send_at FROM scheduled WHERE sent = 0")
    for schedule_id, send_at_str in rows:
        send_at = datetime.fromisoformat(send_at_str)
        delay = max((send_at - now).total_seconds(), 0)  # overdue -> send immediately
        application.job_queue.run_once(
            run_scheduled_job, when=delay, data={"schedule_id": schedule_id}, name=f"sched_{schedule_id}"
        )
    if rows:
        logger.info(f"Re-armed {len(rows)} pending scheduled announcement(s).")


# ------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------

def main() -> None:
    if BOT_TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
        raise SystemExit("Set BOT_TOKEN as an environment variable or in this file.")
    if not ADMIN_IDS:
        raise SystemExit(
            "Set ADMIN_IDS as an environment variable, e.g. ADMIN_IDS=123456789 "
            "(comma-separate multiple IDs). Get your ID from @userinfobot on Telegram."
        )

    init_db()

    app = Application.builder().token(BOT_TOKEN).post_init(
        lambda application: rearm_pending_jobs(application)
    ).build()

    broadcast_conv = ConversationHandler(
        entry_points=[CommandHandler("broadcast", broadcast_start)],
        states={
            CONTENT: [MessageHandler(filters.ALL & ~filters.COMMAND, broadcast_content)],
            BUTTONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, broadcast_buttons)],
            TARGETS: [CallbackQueryHandler(broadcast_targets_toggle)],
            SCHEDULE: [MessageHandler(filters.TEXT & ~filters.COMMAND, broadcast_schedule)],
        },
        fallbacks=[CommandHandler("cancel", broadcast_cancel)],
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("myid", myid_command))
    app.add_handler(CommandHandler("subscribe", subscribe_command))
    app.add_handler(CommandHandler("unsubscribe", unsubscribe_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(broadcast_conv)
    app.add_handler(ChatMemberHandler(on_bot_membership_change, ChatMemberHandler.MY_CHAT_MEMBER))

    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
