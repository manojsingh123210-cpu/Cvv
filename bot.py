"""
CYTUN REFER BOT (XYVEN PREMIUM REFER & EARN)
Single-file production bot: aiogram 3.28.2 + Flask + SQLite.

ALL FIXES INCLUDED:
  - DB path auto-fallback to /tmp if /var/data not writable
  - bot.me() validation on startup + BOT_USERNAME cache
  - safe_send / safe_edit: 4-level progressive fallback (never silently fails)
  - Webhook header secret validation (X-Telegram-Bot-Api-Secret-Token)
  - cmd_start wrapped in try/except (normal users always get a response)
  - Startup channel admin check (logs warning if bot not admin in channel)
  - HTML escape for error messages to prevent parse errors
  - BOT_NAME defaults to "CYTUN REFER BOT"

Run with exactly ONE worker:
  gunicorn -w 1 -k gthread --threads 8 --timeout 120 bot:app
"""

from __future__ import annotations

import asyncio
import html as _html_escape
import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from dotenv import load_dotenv
from flask import Flask, jsonify, make_response, render_template_string, request

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# 1. CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

BOT_NAME = os.getenv("BOT_NAME", "CYTUN REFER BOT").strip()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip().lstrip("@")

ADMIN_USERNAMES = ["@cytunn", "@R0syug"]

_raw_admins = os.getenv("ADMIN_IDS", "").replace(" ", "")
ADMIN_IDS: set[int] = set()
for part in _raw_admins.split(","):
    if part.isdigit():
        ADMIN_IDS.add(int(part))

MAIN_CHANNEL = "https://t.me/whymeyarrr"
MAIN_CHANNEL_USERNAME = "@whymeyarrr"

REFERRAL_REWARD_PAISE = 200          # ₹2.00
MIN_WITHDRAWAL_PAISE = 1000          # ₹10.00
CURRENCY = "INR"

DATABASE_PATH = os.getenv("DATABASE_PATH", "xyven.db")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
WEBSITE_URL = os.getenv("WEBSITE_URL", "").rstrip("/")

# ─────────────────────────────────────────────────────────────────────────────
# 2. CUSTOM EMOJI IDS
# ─────────────────────────────────────────────────────────────────────────────

EMOJI_IDS = [
    "5357592447557848986", "5258387666616994756", "5391118489824015979",
    "5334665104677941170", "5289862389552919154", "5397730656400714154",
    "5301275719681190738", "5301096984617166561", "5386521874089914548",
    "5332423642850536254", "5264892613630111886", "5258113901106580375",
    "5258212320282168974", "5258274739041883702", "5258457030338844822",
    "5260373152688540294", "5258420785109830474", "5258178411515382761",
    "5260691173541970181", "6014620343883273557", "6015104451122042405",
    "6104794084205925773", "6104675564583392962", "6107015024679592166",
    "6120548140211770562",
]

FALLBACK_EMOJI = [
    "✨", "👑", "🌟", "💰", "👥", "💸", "📊", "🎁", "🔗", "💎",
    "📜", "💬", "🛠️", "📢", "➕", "➖", "🚪", "✅", "❌", "⚠️",
    "🎉", "🚀", "📅", "🔍", "📋",
]

E_STAR, E_CROWN, E_WELCOME, E_MONEY, E_USERS = 0, 1, 2, 3, 4
E_WITHDRAW, E_STATS, E_GIFT, E_LINK, E_BALANCE = 5, 6, 7, 8, 9
E_HISTORY, E_SUPPORT, E_ADMIN, E_BROADCAST, E_PROMO = 10, 11, 12, 13, 14
E_ADD, E_REMOVE, E_CLOSE, E_APPROVE, E_REJECT = 15, 16, 17, 18, 19
E_WARN, E_ERROR, E_SUCCESS, E_CALENDAR, E_SEARCH = 20, 21, 22, 23, 24


def ce(index: int, fallback: Optional[str] = None) -> str:
    eid = EMOJI_IDS[index % len(EMOJI_IDS)]
    fb = fallback or FALLBACK_EMOJI[index % len(FALLBACK_EMOJI)]
    return f'<tg-emoji emoji-id="{eid}">{fb}</tg-emoji>'


def plain(index: int) -> str:
    return FALLBACK_EMOJI[index % len(FALLBACK_EMOJI)]


# ─────────────────────────────────────────────────────────────────────────────
# 3. LOGGING
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
log = logging.getLogger("xyven")


# ─────────────────────────────────────────────────────────────────────────────
# DB PATH VALIDATION
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_db_dir() -> None:
    global DATABASE_PATH
    db_dir = os.path.dirname(os.path.abspath(DATABASE_PATH)) or "."
    try:
        os.makedirs(db_dir, exist_ok=True)
        test = os.path.join(db_dir, ".write_test")
        with open(test, "w") as f:
            f.write("ok")
        os.remove(test)
        log.info("Database directory OK: %s", db_dir)
    except (PermissionError, OSError) as exc:
        fallback = "/tmp/xyven.db"
        log.warning("DB directory %s not writable (%s). Falling back to %s",
                    db_dir, exc, fallback)
        DATABASE_PATH = fallback


# ─────────────────────────────────────────────────────────────────────────────
# 4. DATABASE
# ─────────────────────────────────────────────────────────────────────────────

_db_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def db(write: bool = False):
    if write:
        _db_lock.acquire()
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()
        if write:
            _db_lock.release()


def init_db() -> None:
    with db(write=True) as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT, first_name TEXT, last_name TEXT,
                referred_by INTEGER,
                is_verified INTEGER NOT NULL DEFAULT 0,
                balance_paise INTEGER NOT NULL DEFAULT 0,
                reserved_paise INTEGER NOT NULL DEFAULT 0,
                total_earned_paise INTEGER NOT NULL DEFAULT 0,
                total_withdrawn_paise INTEGER NOT NULL DEFAULT 0,
                successful_referrals INTEGER NOT NULL DEFAULT 0,
                total_referrals INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_users_referred_by ON users(referred_by);
            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL,
                referred_id INTEGER NOT NULL UNIQUE,
                reward_paise INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'credited',
                created_at TEXT NOT NULL,
                FOREIGN KEY (referrer_id) REFERENCES users(user_id),
                FOREIGN KEY (referred_id) REFERENCES users(user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id);
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                amount_paise INTEGER NOT NULL,
                description TEXT, reference_id TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_tx_user ON transactions(user_id, id DESC);
            CREATE TABLE IF NOT EXISTS withdrawals (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                amount_paise INTEGER NOT NULL,
                upi_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                requested_at TEXT NOT NULL,
                processed_at TEXT, processed_by INTEGER,
                rejection_reason TEXT,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_wd_status ON withdrawals(status, requested_at);
            CREATE TABLE IF NOT EXISTS promo_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_link TEXT NOT NULL,
                chat_identifier TEXT NOT NULL,
                title TEXT, added_by INTEGER,
                added_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS website_visits (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                added_at TEXT NOT NULL
            );
            """
        )
        c.execute("INSERT OR IGNORE INTO website_visits (id, count) VALUES (1, 0)")
        c.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fmt_money(paise: int) -> str:
    return f"₹{paise / 100:,.2f}"


def get_user(user_id: int) -> Optional[sqlite3.Row]:
    with db() as c:
        return c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def register_user(user_id, username, first_name, last_name, referred_by) -> tuple[bool, str]:
    with db(write=True) as c:
        existing = c.execute("SELECT is_verified FROM users WHERE user_id=?",
                             (user_id,)).fetchone()
        if existing:
            return bool(existing["is_verified"]), "exists"
        c.execute(
            """INSERT INTO users (user_id, username, first_name, last_name,
               referred_by, is_verified, created_at) VALUES (?,?,?,?,?,0,?)""",
            (user_id, username, first_name, last_name, referred_by, _now()),
        )
        if referred_by and referred_by != user_id:
            c.execute("UPDATE users SET total_referrals=total_referrals+1 WHERE user_id=?",
                      (referred_by,))
        c.commit()
        return False, "created"


def mark_verified_and_credit(user_id: int) -> tuple[bool, Optional[int], int]:
    with db(write=True) as c:
        row = c.execute("SELECT referred_by, is_verified FROM users WHERE user_id=?",
                        (user_id,)).fetchone()
        if not row:
            return False, None, 0
        if row["is_verified"]:
            return False, None, 0
        c.execute("UPDATE users SET is_verified=1 WHERE user_id=?", (user_id,))
        referrer_id = row["referred_by"]
        reward = 0
        if referrer_id and referrer_id != user_id:
            referrer = c.execute("SELECT user_id FROM users WHERE user_id=?",
                                 (referrer_id,)).fetchone()
            already = c.execute("SELECT id FROM referrals WHERE referred_id=?",
                                (user_id,)).fetchone()
            if referrer and not already:
                c.execute(
                    """INSERT INTO referrals (referrer_id, referred_id, reward_paise,
                       status, created_at) VALUES (?,?,?,'credited',?)""",
                    (referrer_id, user_id, REFERRAL_REWARD_PAISE, _now()),
                )
                c.execute(
                    """UPDATE users SET
                       balance_paise = balance_paise + ?,
                       total_earned_paise = total_earned_paise + ?,
                       successful_referrals = successful_referrals + 1
                       WHERE user_id=?""",
                    (REFERRAL_REWARD_PAISE, REFERRAL_REWARD_PAISE, referrer_id),
                )
                c.execute(
                    """INSERT INTO transactions (user_id, type, amount_paise,
                       description, created_at) VALUES (?,?,?,?,?)""",
                    (referrer_id, "referral_reward", REFERRAL_REWARD_PAISE,
                     f"Referral reward for user {user_id}", _now()),
                )
                reward = REFERRAL_REWARD_PAISE
        c.commit()
        return True, referrer_id, reward


def list_promo_channels() -> list[sqlite3.Row]:
    with db() as c:
        return c.execute("SELECT * FROM promo_channels ORDER BY id").fetchall()


def add_promo_channel(link, identifier, title, added_by) -> None:
    with db(write=True) as c:
        c.execute(
            """INSERT INTO promo_channels (channel_link, chat_identifier, title,
               added_by, added_at) VALUES (?,?,?,?,?)""",
            (link, identifier, title, added_by, _now()),
        )
        c.commit()


def remove_promo_channel(channel_id: int) -> None:
    with db(write=True) as c:
        c.execute("DELETE FROM promo_channels WHERE id=?", (channel_id,))
        c.commit()


def increment_visit() -> int:
    with db(write=True) as c:
        c.execute("UPDATE website_visits SET count = count + 1 WHERE id = 1")
        c.commit()
        row = c.execute("SELECT count FROM website_visits WHERE id=1").fetchone()
        return int(row["count"]) if row else 0


def get_visit_count() -> int:
    with db() as c:
        row = c.execute("SELECT count FROM website_visits WHERE id=1").fetchone()
        return int(row["count"]) if row else 0


def create_withdrawal(wd_id, user_id, amount_paise, upi_id) -> tuple[bool, str]:
    with db(write=True) as c:
        user = c.execute("SELECT balance_paise FROM users WHERE user_id=?",
                         (user_id,)).fetchone()
        if not user:
            return False, "no_user"
        if user["balance_paise"] < amount_paise:
            return False, "insufficient"
        c.execute(
            "UPDATE users SET balance_paise = balance_paise - ?, "
            "reserved_paise = reserved_paise + ? WHERE user_id=?",
            (amount_paise, amount_paise, user_id),
        )
        c.execute(
            """INSERT INTO withdrawals (id, user_id, amount_paise, upi_id,
               status, requested_at) VALUES (?,?,?,?,'pending',?)""",
            (wd_id, user_id, amount_paise, upi_id, _now()),
        )
        c.execute(
            """INSERT INTO transactions (user_id, type, amount_paise, description,
               reference_id, created_at) VALUES (?,?,?,?,?,?)""",
            (user_id, "withdrawal_hold", -amount_paise,
             f"Withdrawal request {wd_id}", wd_id, _now()),
        )
        c.commit()
        return True, "ok"


def approve_withdrawal(wd_id, admin_id) -> tuple[bool, str, Optional[sqlite3.Row]]:
    with db(write=True) as c:
        wd = c.execute("SELECT * FROM withdrawals WHERE id=?", (wd_id,)).fetchone()
        if not wd:
            return False, "not_found", None
        if wd["status"] != "pending":
            return False, f"already_{wd['status']}", wd
        c.execute(
            "UPDATE withdrawals SET status='approved', processed_at=?, processed_by=? "
            "WHERE id=? AND status='pending'",
            (_now(), admin_id, wd_id),
        )
        c.commit()
        updated = c.execute("SELECT * FROM withdrawals WHERE id=?", (wd_id,)).fetchone()
        return True, "approved", updated


def mark_withdrawal_paid(wd_id, admin_id) -> tuple[bool, str, Optional[sqlite3.Row]]:
    with db(write=True) as c:
        wd = c.execute("SELECT * FROM withdrawals WHERE id=?", (wd_id,)).fetchone()
        if not wd:
            return False, "not_found", None
        if wd["status"] == "paid":
            return False, "already_paid", wd
        if wd["status"] != "approved":
            return False, f"not_approved_{wd['status']}", wd
        c.execute(
            "UPDATE withdrawals SET status='paid', processed_at=?, processed_by=? "
            "WHERE id=? AND status='approved'",
            (_now(), admin_id, wd_id),
        )
        c.execute(
            "UPDATE users SET reserved_paise = reserved_paise - ?, "
            "total_withdrawn_paise = total_withdrawn_paise + ? WHERE user_id=?",
            (wd["amount_paise"], wd["amount_paise"], wd["user_id"]),
        )
        c.execute(
            """INSERT INTO transactions (user_id, type, amount_paise, description,
               reference_id, created_at) VALUES (?,?,?,?,?,?)""",
            (wd["user_id"], "withdrawal_paid", 0,
             f"Withdrawal {wd_id} paid", wd_id, _now()),
        )
        c.commit()
        updated = c.execute("SELECT * FROM withdrawals WHERE id=?", (wd_id,)).fetchone()
        return True, "paid", updated


def reject_withdrawal(wd_id, admin_id, reason="") -> tuple[bool, str, Optional[sqlite3.Row]]:
    with db(write=True) as c:
        wd = c.execute("SELECT * FROM withdrawals WHERE id=?", (wd_id,)).fetchone()
        if not wd:
            return False, "not_found", None
        if wd["status"] != "pending":
            return False, f"already_{wd['status']}", wd
        c.execute(
            """UPDATE withdrawals SET status='rejected', processed_at=?,
               processed_by=?, rejection_reason=? WHERE id=? AND status='pending'""",
            (_now(), admin_id, reason or "Rejected by admin", wd_id),
        )
        c.execute(
            "UPDATE users SET balance_paise = balance_paise + ?, "
            "reserved_paise = reserved_paise - ? WHERE user_id=?",
            (wd["amount_paise"], wd["amount_paise"], wd["user_id"]),
        )
        c.execute(
            """INSERT INTO transactions (user_id, type, amount_paise, description,
               reference_id, created_at) VALUES (?,?,?,?,?,?)""",
            (wd["user_id"], "withdrawal_refund", wd["amount_paise"],
             f"Withdrawal {wd_id} rejected and refunded", wd_id, _now()),
        )
        c.commit()
        updated = c.execute("SELECT * FROM withdrawals WHERE id=?", (wd_id,)).fetchone()
        return True, "rejected", updated


def list_pending_withdrawals(limit=10) -> list[sqlite3.Row]:
    with db() as c:
        return c.execute(
            "SELECT * FROM withdrawals WHERE status='pending' "
            "ORDER BY requested_at DESC LIMIT ?", (limit,)
        ).fetchall()


def user_transactions(user_id, limit=20) -> list[sqlite3.Row]:
    with db() as c:
        return c.execute(
            "SELECT * FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()


def admin_stats() -> dict[str, Any]:
    with db() as c:
        row = c.execute(
            """SELECT
                 (SELECT COUNT(*) FROM users) AS total_users,
                 (SELECT COUNT(*) FROM referrals) AS total_referrals,
                 (SELECT COUNT(*) FROM referrals WHERE status='credited') AS successful_referrals,
                 (SELECT COALESCE(SUM(reward_paise),0) FROM referrals WHERE status='credited') AS total_referral_rewards,
                 (SELECT COUNT(*) FROM withdrawals WHERE status='pending') AS pending_withdrawals,
                 (SELECT COUNT(*) FROM withdrawals WHERE status='approved') AS approved_withdrawals,
                 (SELECT COUNT(*) FROM withdrawals WHERE status='paid') AS paid_withdrawals,
                 (SELECT COALESCE(SUM(balance_paise),0) FROM users) AS total_available_balance,
                 (SELECT COALESCE(SUM(reserved_paise),0) FROM users) AS total_reserved_balance
            """
        ).fetchone()
        return dict(row)


def list_users_page(page, per_page=10) -> list[sqlite3.Row]:
    offset = max(0, page) * per_page
    with db() as c:
        return c.execute(
            "SELECT * FROM users ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (per_page, offset),
        ).fetchall()


def count_users() -> int:
    with db() as c:
        return int(c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"])


# ─────────────────────────────────────────────────────────────────────────────
# 5. BUTTON HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def btn(text, callback_data=None, *, style=None, emoji_index=None, url=None,
        copy_text=None) -> InlineKeyboardButton:
    kwargs: dict[str, Any] = {"text": text}
    if callback_data is not None:
        kwargs["callback_data"] = callback_data
    if url is not None:
        kwargs["url"] = url
    if copy_text is not None:
        kwargs["copy_text"] = CopyTextButton(text=copy_text)
    if style:
        kwargs["style"] = style
    if emoji_index is not None:
        kwargs["icon_custom_emoji_id"] = EMOJI_IDS[emoji_index % len(EMOJI_IDS)]
    return InlineKeyboardButton(**kwargs)


def strip_button_icons(markup) -> Optional[InlineKeyboardMarkup]:
    if not markup or not isinstance(markup, InlineKeyboardMarkup):
        return markup
    rows: list[list[InlineKeyboardButton]] = []
    for row in markup.inline_keyboard:
        rows.append([b.model_copy(update={"icon_custom_emoji_id": None}) for b in row])
    return InlineKeyboardMarkup(inline_keyboard=rows)


_CUSTOM_EMOJI_TAG_RE = re.compile(r"<tg-emoji[^>]*>(.*?)</tg-emoji>", re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def strip_custom_emoji(text: str) -> str:
    return _CUSTOM_EMOJI_TAG_RE.sub(r"\1", text)


def strip_all_html(text: str) -> str:
    return _HTML_TAG_RE.sub("", text)


# ─────────────────────────────────────────────────────────────────────────────
# SAFE SEND / EDIT — 4-level progressive fallback
# ─────────────────────────────────────────────────────────────────────────────

async def safe_send(bot: Bot, chat_id: int, text: str,
                    reply_markup: Optional[InlineKeyboardMarkup] = None,
                    **kwargs: Any) -> Optional[Message]:
    # Attempt 1 — full premium
    try:
        return await bot.send_message(chat_id, text, reply_markup=reply_markup, **kwargs)
    except TelegramBadRequest as exc1:
        log.warning("safe_send attempt 1 failed: %s", exc1)
    # Attempt 2 — strip custom emoji + button icons
    try:
        clean_text = strip_custom_emoji(text)
        clean_markup = strip_button_icons(reply_markup)
        return await bot.send_message(chat_id, clean_text, reply_markup=clean_markup, **kwargs)
    except TelegramBadRequest as exc2:
        log.warning("safe_send attempt 2 failed: %s", exc2)
    # Attempt 3 — strip ALL HTML
    try:
        plain_text = strip_all_html(strip_custom_emoji(text))
        clean_markup = strip_button_icons(reply_markup)
        return await bot.send_message(chat_id, plain_text, reply_markup=clean_markup,
                                      parse_mode=None, **kwargs)
    except TelegramBadRequest as exc3:
        log.warning("safe_send attempt 3 failed: %s", exc3)
    # Attempt 4 — plain text only
    try:
        plain = strip_all_html(strip_custom_emoji(text))
        return await bot.send_message(chat_id, plain, parse_mode=None, **kwargs)
    except Exception as exc4:
        log.exception("safe_send COMPLETELY FAILED for chat %s: %s", chat_id, exc4)
        return None


async def safe_edit(bot: Bot, chat_id: int, message_id: int, text: str,
                    reply_markup: Optional[InlineKeyboardMarkup] = None,
                    **kwargs: Any) -> Optional[Message]:
    # Attempt 1
    try:
        return await bot.edit_message_text(text=text, chat_id=chat_id,
                                           message_id=message_id,
                                           reply_markup=reply_markup, **kwargs)
    except TelegramBadRequest as exc1:
        err = str(exc1).lower()
        if "message is not modified" in err:
            return None
        log.warning("safe_edit attempt 1 failed: %s", exc1)
    # Attempt 2
    try:
        clean_text = strip_custom_emoji(text)
        clean_markup = strip_button_icons(reply_markup)
        return await bot.edit_message_text(text=clean_text, chat_id=chat_id,
                                           message_id=message_id,
                                           reply_markup=clean_markup, **kwargs)
    except TelegramBadRequest as exc2:
        log.warning("safe_edit attempt 2 failed: %s", exc2)
    # Attempt 3
    try:
        plain_text = strip_all_html(strip_custom_emoji(text))
        clean_markup = strip_button_icons(reply_markup)
        return await bot.edit_message_text(text=plain_text, chat_id=chat_id,
                                           message_id=message_id,
                                           reply_markup=clean_markup,
                                           parse_mode=None, **kwargs)
    except Exception as exc3:
        log.warning("safe_edit completely failed: %s", exc3)
        return None


async def safe_answer(callback: CallbackQuery, text: str, alert: bool = False) -> None:
    try:
        await callback.answer(text, show_alert=alert)
    except TelegramBadRequest:
        try:
            await callback.answer()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# 6. BOT / DISPATCHER
# ─────────────────────────────────────────────────────────────────────────────

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

router = Router()
dp = Dispatcher(storage=MemoryStorage())
dp.include_router(router)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

_bot_username_cache: Optional[str] = BOT_USERNAME or None


def referral_link(user_id: int) -> str:
    username = _bot_username_cache or BOT_USERNAME or "YOUR_BOT"
    return f"https://t.me/{username}?start={user_id}"


# ─────────────────────────────────────────────────────────────────────────────
# 7. FORCE JOIN
# ─────────────────────────────────────────────────────────────────────────────

async def check_force_join(user_id: int) -> tuple[list[tuple[str, str]], list[str]]:
    required: list[tuple[str, str]] = [("Main Channel", MAIN_CHANNEL_USERNAME)]
    for ch in list_promo_channels():
        required.append((ch["title"] or ch["channel_link"], ch["chat_identifier"]))
    missing: list[tuple[str, str]] = []
    errors: list[str] = []
    for title, chat_id in required:
        try:
            member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            if member.status in ("left", "kicked"):
                missing.append((title, chat_id))
        except TelegramBadRequest as exc:
            errors.append(f"{title}: {exc.message}")
            log.warning("Force-join check failed for %s: %s", chat_id, exc)
    return missing, errors


def force_join_kb(missing: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for title, chat_id in missing:
        link = chat_id if chat_id.startswith("@") else str(chat_id)
        if chat_id.startswith("@"):
            link = f"https://t.me/{chat_id.lstrip('@')}"
        rows.append([btn(f"JOIN {title.upper()}", url=link, style="primary",
                         emoji_index=E_LINK)])
    rows.append([btn("VERIFY JOIN", callback_data="verify_join",
                     style="success", emoji_index=E_APPROVE)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ─────────────────────────────────────────────────────────────────────────────
# 8. MAIN MENU
# ─────────────────────────────────────────────────────────────────────────────

def main_menu_kb(user_id: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [btn("MY ACCOUNT", callback_data="account", style="primary", emoji_index=E_STAR)],
        [btn("REFER & EARN", callback_data="refer", style="success", emoji_index=E_USERS)],
        [btn("MY BALANCE", callback_data="balance", style="success", emoji_index=E_BALANCE)],
        [btn("WITHDRAW", callback_data="withdraw", style="primary", emoji_index=E_WITHDRAW),
         btn("STATISTICS", callback_data="stats", style="success", emoji_index=E_STATS)],
        [btn("HOW TO EARN", callback_data="how_to_earn", style="primary", emoji_index=E_HISTORY),
         btn("SUPPORT", callback_data="support", style="success", emoji_index=E_SUPPORT)],
    ]
    if user_id in ADMIN_IDS:
        rows.append([btn("ADMIN PANEL", callback_data="admin_panel",
                         style="danger", emoji_index=E_ADMIN)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def welcome_text() -> str:
    return (
        f"{ce(E_STAR)} <b>{BOT_NAME}</b> {ce(E_CROWN)}\n\n"
        f"{ce(E_WELCOME)} Welcome to your personal earning platform.\n\n"
        f"{ce(E_MONEY)} Earn <b>₹2</b> for every successful referral.\n"
        f"{ce(E_USERS)} Invite friends, grow your balance, and withdraw once you reach <b>₹10</b>.\n\n"
        f"{ce(E_GIFT)} Your premium earning journey starts now."
    )


async def show_main_menu(chat_id: int, user_id: int,
                         edit_message_id: Optional[int] = None) -> None:
    if edit_message_id:
        await safe_edit(bot, chat_id, edit_message_id, welcome_text(),
                        reply_markup=main_menu_kb(user_id))
    else:
        await safe_send(bot, chat_id, welcome_text(), reply_markup=main_menu_kb(user_id))


# ─────────────────────────────────────────────────────────────────────────────
# 9. /START — CRASH-PROOF
# ─────────────────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    try:
        await _handle_start(message, state)
    except Exception as exc:
        log.exception("cmd_start CRASHED for user %s: %s",
                      message.from_user.id if message.from_user else "?", exc)
        try:
            await bot.send_message(
                message.chat.id,
                "⚠️ Kuch technical issue aa gayi. Please /start dobara bhejo.",
                parse_mode=None,
            )
        except Exception:
            pass


async def _handle_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    u = message.from_user
    if not u:
        return

    payload = message.text.split(maxsplit=1) if message.text else []
    referred_by: Optional[int] = None
    if len(payload) > 1 and payload[1].isdigit():
        candidate = int(payload[1])
        if candidate != u.id:
            referred_by = candidate

    log.info("START from user %s (@%s) referred_by=%s", u.id, u.username, referred_by)

    await asyncio.to_thread(register_user, u.id, u.username, u.first_name,
                            u.last_name, referred_by)

    missing, errors = await check_force_join(u.id)
    log.info("Force-join check: missing=%d errors=%d", len(missing), len(errors))

    if missing:
        await safe_send(
            bot, message.chat.id,
            f"{ce(E_WARN)} <b>JOIN REQUIRED</b>\n\n"
            f"{ce(E_LINK)} Please join the channel(s) below to unlock {BOT_NAME}.\n"
            f"{ce(E_CROWN)} After joining, tap <b>VERIFY JOIN</b>.",
            reply_markup=force_join_kb(missing),
        )
        return

    if errors:
        safe_errors = "\n".join(f"• {_html_escape.escape(e)}" for e in errors)
        await safe_send(
            bot, message.chat.id,
            f"{ce(E_WARN)} <b>Membership check warning</b>\n\n"
            f"{ce(E_ERROR)} {safe_errors}\n\n"
            f"{ce(E_WARN)} Make sure the bot is an administrator in the channel.",
        )

    newly_verified, referrer_id, reward = await asyncio.to_thread(
        mark_verified_and_credit, u.id)
    if newly_verified and referrer_id and reward:
        try:
            await safe_send(
                bot, referrer_id,
                f"{ce(E_SUCCESS)} <b>NEW REFERRAL!</b>\n\n"
                f"{ce(E_USERS)} {u.first_name or 'A user'} joined using your link.\n"
                f"{ce(E_MONEY)} You earned <b>{fmt_money(reward)}</b>.",
            )
        except Exception as exc:
            log.warning("Failed to notify referrer %s: %s", referrer_id, exc)

    await show_main_menu(message.chat.id, u.id)


@router.callback_query(F.data == "verify_join")
async def cb_verify_join(callback: CallbackQuery, state: FSMContext) -> None:
    u = callback.from_user
    missing, errors = await check_force_join(u.id)

    if missing:
        await safe_edit(
            bot, callback.message.chat.id, callback.message.message_id,
            f"{ce(E_WARN)} <b>JOIN REQUIRED</b>\n\n"
            f"{ce(E_LINK)} You still need to join:\n"
            + "\n".join(f"• {t}" for t, _ in missing)
            + f"\n\n{ce(E_CROWN)} Tap <b>VERIFY JOIN</b> after joining.",
            reply_markup=force_join_kb(missing),
        )
        await safe_answer(callback, "You have not joined all required channels.", alert=True)
        return

    if errors:
        safe_errors = "\n".join(f"• {_html_escape.escape(e)}" for e in errors)
        await safe_answer(callback, "Cannot verify membership. Contact support.", alert=True)
        await safe_send(
            bot, callback.message.chat.id,
            f"{ce(E_WARN)} <b>Verification failed</b>\n\n{safe_errors}\n\n"
            f"{ce(E_ERROR)} The bot must be an admin in the channel.",
        )
        return

    newly_verified, referrer_id, reward = await asyncio.to_thread(
        mark_verified_and_credit, u.id)
    if newly_verified and referrer_id and reward:
        try:
            await safe_send(
                bot, referrer_id,
                f"{ce(E_SUCCESS)} <b>NEW REFERRAL!</b>\n\n"
                f"{ce(E_USERS)} {u.first_name or 'A user'} joined using your link.\n"
                f"{ce(E_MONEY)} You earned <b>{fmt_money(reward)}</b>.",
            )
        except Exception:
            pass

    await safe_answer(callback, "Verified successfully!", alert=False)
    await show_main_menu(callback.message.chat.id, u.id,
                         edit_message_id=callback.message.message_id)


# ─────────────────────────────────────────────────────────────────────────────
# 10. ACCOUNT / REFER / BALANCE / STATS / HELP
# ─────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "account")
async def cb_account(callback: CallbackQuery) -> None:
    user = await asyncio.to_thread(get_user, callback.from_user.id)
    if not user:
        await safe_answer(callback, "Please /start first.", alert=True)
        return
    text = (
        f"{ce(E_STAR)} <b>MY ACCOUNT</b> {ce(E_CROWN)}\n\n"
        f"{ce(E_USERS)} <b>Name:</b> {user['first_name'] or '—'}\n"
        f"{ce(E_SUPPORT)} <b>Telegram ID:</b> <code>{user['user_id']}</code>\n"
        f"{ce(E_CALENDAR)} <b>Registered:</b> {user['created_at'][:10]}\n"
        f"{ce(E_LINK)} <b>Referral link:</b>\n<code>{referral_link(user['user_id'])}</code>\n\n"
        f"{ce(E_USERS)} <b>Total referrals:</b> {user['total_referrals']}\n"
        f"{ce(E_SUCCESS)} <b>Successful referrals:</b> {user['successful_referrals']}\n"
        f"{ce(E_MONEY)} <b>Total referral earnings:</b> {fmt_money(user['total_earned_paise'])}\n"
        f"{ce(E_BALANCE)} <b>Available balance:</b> {fmt_money(user['balance_paise'])}\n"
        f"{ce(E_WITHDRAW)} <b>Pending withdrawal:</b> {fmt_money(user['reserved_paise'])}\n"
        f"{ce(E_HISTORY)} <b>Total paid:</b> {fmt_money(user['total_withdrawn_paise'])}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("COPY REFERRAL LINK", copy_text=referral_link(user["user_id"]),
             style="primary", emoji_index=E_LINK)],
        [btn("REFER NOW", callback_data="refer", style="success", emoji_index=E_USERS)],
        [btn("BACK", callback_data="back_main", style="danger", emoji_index=E_REMOVE)],
    ])
    await safe_edit(bot, callback.message.chat.id, callback.message.message_id, text, kb)


@router.callback_query(F.data == "refer")
async def cb_refer(callback: CallbackQuery) -> None:
    user = await asyncio.to_thread(get_user, callback.from_user.id)
    if not user:
        await safe_answer(callback, "Please /start first.", alert=True)
        return
    link = referral_link(user["user_id"])
    share_text = f"Join {BOT_NAME} and earn ₹2 per referral!"
    share_url = f"https://t.me/share/url?url={link}&text={share_text}"
    text = (
        f"{ce(E_USERS)} <b>REFER & EARN</b> {ce(E_GIFT)}\n\n"
        f"{ce(E_MONEY)} Earn <b>₹2</b> for every friend who joins and verifies.\n\n"
        f"{ce(E_LINK)} <b>Your link:</b>\n<code>{link}</code>\n\n"
        f"{ce(E_STATS)} <b>Successful referrals:</b> {user['successful_referrals']}\n"
        f"{ce(E_BALANCE)} <b>Referral earnings:</b> {fmt_money(user['total_earned_paise'])}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("COPY LINK", copy_text=link, style="primary", emoji_index=E_LINK)],
        [btn("SHARE LINK", url=share_url, style="success", emoji_index=E_GIFT)],
        [btn("BACK", callback_data="back_main", style="danger", emoji_index=E_REMOVE)],
    ])
    await safe_edit(bot, callback.message.chat.id, callback.message.message_id, text, kb)


@router.callback_query(F.data == "balance")
async def cb_balance(callback: CallbackQuery) -> None:
    user = await asyncio.to_thread(get_user, callback.from_user.id)
    if not user:
        await safe_answer(callback, "Please /start first.", alert=True)
        return
    text = (
        f"{ce(E_BALANCE)} <b>MY BALANCE</b> {ce(E_MONEY)}\n\n"
        f"{ce(E_MONEY)} <b>Total earned:</b> {fmt_money(user['total_earned_paise'])}\n"
        f"{ce(E_SUCCESS)} <b>Available balance:</b> {fmt_money(user['balance_paise'])}\n"
        f"{ce(E_WITHDRAW)} <b>Reserved balance:</b> {fmt_money(user['reserved_paise'])}\n"
        f"{ce(E_HISTORY)} <b>Pending withdrawals:</b> {fmt_money(user['reserved_paise'])}\n"
        f"{ce(E_APPROVE)} <b>Total paid:</b> {fmt_money(user['total_withdrawn_paise'])}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("WITHDRAW", callback_data="withdraw", style="success", emoji_index=E_WITHDRAW)],
        [btn("TRANSACTION HISTORY", callback_data="history", style="primary", emoji_index=E_HISTORY)],
        [btn("BACK", callback_data="back_main", style="danger", emoji_index=E_REMOVE)],
    ])
    await safe_edit(bot, callback.message.chat.id, callback.message.message_id, text, kb)


@router.callback_query(F.data == "history")
async def cb_history(callback: CallbackQuery) -> None:
    txs = await asyncio.to_thread(user_transactions, callback.from_user.id)
    lines = [f"{ce(E_HISTORY)} <b>TRANSACTION HISTORY</b>\n"]
    if not txs:
        lines.append(f"{ce(E_WARN)} No transactions yet.")
    else:
        for t in txs:
            sign = "+" if t["amount_paise"] >= 0 else ""
            lines.append(
                f"{ce(E_CALENDAR)} {t['created_at'][:16].replace('T', ' ')} — "
                f"<b>{sign}{fmt_money(t['amount_paise'])}</b>\n"
                f"   {ce(E_STAR)} {t['type']}: {t['description'] or ''}"
            )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("BACK", callback_data="balance", style="danger", emoji_index=E_REMOVE)]
    ])
    await safe_edit(bot, callback.message.chat.id, callback.message.message_id,
                    "\n".join(lines), kb)


@router.callback_query(F.data == "stats")
async def cb_stats(callback: CallbackQuery) -> None:
    user = await asyncio.to_thread(get_user, callback.from_user.id)
    if not user:
        await safe_answer(callback, "Please /start first.", alert=True)
        return
    stats = await asyncio.to_thread(admin_stats)
    text = (
        f"{ce(E_STATS)} <b>GLOBAL STATISTICS</b> {ce(E_CROWN)}\n\n"
        f"{ce(E_USERS)} <b>Total users:</b> {stats['total_users']}\n"
        f"{ce(E_LINK)} <b>Total referrals:</b> {stats['total_referrals']}\n"
        f"{ce(E_SUCCESS)} <b>Successful referrals:</b> {stats['successful_referrals']}\n"
        f"{ce(E_MONEY)} <b>Referral rewards paid:</b> {fmt_money(stats['total_referral_rewards'])}\n"
        f"{ce(E_WITHDRAW)} <b>Pending withdrawals:</b> {stats['pending_withdrawals']}\n"
        f"{ce(E_APPROVE)} <b>Paid withdrawals:</b> {stats['paid_withdrawals']}\n\n"
        f"{ce(E_BALANCE)} <b>Your balance:</b> {fmt_money(user['balance_paise'])}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("BACK", callback_data="back_main", style="danger", emoji_index=E_REMOVE)]
    ])
    await safe_edit(bot, callback.message.chat.id, callback.message.message_id, text, kb)


@router.callback_query(F.data == "how_to_earn")
async def cb_how_to_earn(callback: CallbackQuery) -> None:
    text = (
        f"{ce(E_HISTORY)} <b>HOW TO EARN</b> {ce(E_GIFT)}\n\n"
        f"{ce(E_STAR)} 1. Share your referral link with friends.\n"
        f"{ce(E_USERS)} 2. Your friend opens the link and starts the bot.\n"
        f"{ce(E_APPROVE)} 3. Your friend joins the required channel(s) and verifies.\n"
        f"{ce(E_MONEY)} 4. You receive <b>₹2.00</b> in your balance.\n\n"
        f"{ce(E_WITHDRAW)} Withdraw once your available balance reaches <b>₹10.00</b>.\n\n"
        f"{ce(E_WARN)} Rules:\n"
        f"• No self-referrals.\n• Each user can only be referred once.\n"
        f"• Rewards are credited exactly once."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("BACK", callback_data="back_main", style="danger", emoji_index=E_REMOVE)]
    ])
    await safe_edit(bot, callback.message.chat.id, callback.message.message_id, text, kb)


@router.callback_query(F.data == "support")
async def cb_support(callback: CallbackQuery) -> None:
    text = (
        f"{ce(E_SUPPORT)} <b>SUPPORT</b>\n\n"
        f"{ce(E_ADMIN)} Contact the administrators:\n"
        + "\n".join(f"• {u}" for u in ADMIN_USERNAMES)
        + f"\n\n{ce(E_WARN)} Never share your bot token or UPI PIN."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("BACK", callback_data="back_main", style="danger", emoji_index=E_REMOVE)]
    ])
    await safe_edit(bot, callback.message.chat.id, callback.message.message_id, text, kb)


@router.callback_query(F.data == "back_main")
async def cb_back_main(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await show_main_menu(callback.message.chat.id, callback.from_user.id,
                         edit_message_id=callback.message.message_id)


# ─────────────────────────────────────────────────────────────────────────────
# 11. WITHDRAWAL FLOW
# ─────────────────────────────────────────────────────────────────────────────

class WithdrawStates(StatesGroup):
    waiting_amount = State()
    waiting_upi = State()
    waiting_confirm = State()


UPI_RE = re.compile(r"^[a-zA-Z0-9.\-_]{2,256}@[a-zA-Z]{2,64}$")


@router.callback_query(F.data == "withdraw")
async def cb_withdraw_start(callback: CallbackQuery, state: FSMContext) -> None:
    user = await asyncio.to_thread(get_user, callback.from_user.id)
    if not user:
        await safe_answer(callback, "Please /start first.", alert=True)
        return
    if user["balance_paise"] < MIN_WITHDRAWAL_PAISE:
        await safe_answer(callback,
            f"Minimum withdrawal is {fmt_money(MIN_WITHDRAWAL_PAISE)}. "
            f"Your balance is {fmt_money(user['balance_paise'])}.", alert=True)
        return
    await state.set_state(WithdrawStates.waiting_amount)
    text = (
        f"{ce(E_WITHDRAW)} <b>WITHDRAW</b> {ce(E_MONEY)}\n\n"
        f"{ce(E_BALANCE)} Available balance: <b>{fmt_money(user['balance_paise'])}</b>\n"
        f"{ce(E_WARN)} Minimum withdrawal: <b>{fmt_money(MIN_WITHDRAWAL_PAISE)}</b>\n\n"
        f"{ce(E_STAR)} Enter the amount you want to withdraw (e.g. <code>10</code> or <code>10.50</code>)."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("CANCEL", callback_data="back_main", style="danger", emoji_index=E_REMOVE)]
    ])
    await safe_edit(bot, callback.message.chat.id, callback.message.message_id, text, kb)


@router.message(WithdrawStates.waiting_amount)
async def withdraw_amount_received(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip().replace("₹", "").replace(",", "")
    try:
        amount = float(raw)
    except ValueError:
        await safe_send(bot, message.chat.id, f"{ce(E_ERROR)} Invalid amount. Enter a number.")
        return
    amount_paise = int(round(amount * 100))
    user = await asyncio.to_thread(get_user, message.from_user.id)
    if amount_paise < MIN_WITHDRAWAL_PAISE:
        await safe_send(bot, message.chat.id,
            f"{ce(E_ERROR)} Minimum withdrawal is <b>{fmt_money(MIN_WITHDRAWAL_PAISE)}</b>. Try again.")
        return
    if not user or amount_paise > user["balance_paise"]:
        await safe_send(bot, message.chat.id,
            f"{ce(E_ERROR)} Amount exceeds your available balance "
            f"({fmt_money(user['balance_paise']) if user else '₹0.00'}).")
        return
    await state.update_data(amount_paise=amount_paise)
    await state.set_state(WithdrawStates.waiting_upi)
    await safe_send(bot, message.chat.id,
        f"{ce(E_MONEY)} Amount: <b>{fmt_money(amount_paise)}</b>\n\n"
        f"{ce(E_WITHDRAW)} Now enter your <b>UPI ID</b> (e.g. <code>name@okhdfcbank</code>).")


@router.message(WithdrawStates.waiting_upi)
async def withdraw_upi_received(message: Message, state: FSMContext) -> None:
    upi = (message.text or "").strip()
    if not UPI_RE.match(upi):
        await safe_send(bot, message.chat.id,
            f"{ce(E_ERROR)} Invalid UPI ID format. Example: <code>name@okhdfcbank</code>")
        return
    data = await state.get_data()
    amount_paise = int(data.get("amount_paise", 0))
    await state.update_data(upi_id=upi)
    await state.set_state(WithdrawStates.waiting_confirm)
    text = (
        f"{ce(E_APPROVE)} <b>CONFIRM WITHDRAWAL</b>\n\n"
        f"{ce(E_MONEY)} Amount: <b>{fmt_money(amount_paise)}</b>\n"
        f"{ce(E_WITHDRAW)} UPI ID: <code>{upi}</code>\n\n"
        f"{ce(E_WARN)} Once confirmed, the amount will be reserved until an admin processes it."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("CONFIRM", callback_data="withdraw_confirm", style="success", emoji_index=E_APPROVE)],
        [btn("CANCEL", callback_data="back_main", style="danger", emoji_index=E_REMOVE)],
    ])
    await safe_send(bot, message.chat.id, text, reply_markup=kb)


@router.callback_query(F.data == "withdraw_confirm", WithdrawStates.waiting_confirm)
async def cb_withdraw_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    amount_paise = int(data.get("amount_paise", 0))
    upi = data.get("upi_id", "")
    user_id = callback.from_user.id
    if amount_paise < MIN_WITHDRAWAL_PAISE or not UPI_RE.match(upi):
        await safe_answer(callback, "Invalid withdrawal data. Please restart.", alert=True)
        await state.clear()
        return
    wd_id = f"WD{int(time.time())}{user_id % 10000}"
    ok, reason = await asyncio.to_thread(create_withdrawal, wd_id, user_id,
                                          amount_paise, upi)
    if not ok:
        await safe_answer(callback, "Insufficient balance or invalid request.", alert=True)
        await state.clear()
        return
    await state.clear()
    user = await asyncio.to_thread(get_user, user_id)
    await safe_edit(bot, callback.message.chat.id, callback.message.message_id,
        f"{ce(E_SUCCESS)} <b>WITHDRAWAL REQUEST CREATED</b>\n\n"
        f"{ce(E_STAR)} Request ID: <code>{wd_id}</code>\n"
        f"{ce(E_MONEY)} Amount: <b>{fmt_money(amount_paise)}</b>\n"
        f"{ce(E_WITHDRAW)} UPI: <code>{upi}</code>\n"
        f"{ce(E_HISTORY)} Status: <b>PENDING</b>\n\n"
        f"{ce(E_WARN)} You will be notified once an admin processes your request.")
    admin_text = (
        f"{ce(E_WITHDRAW)} <b>WITHDRAWAL REQUEST</b>\n\n"
        f"{ce(E_USERS)} User ID: <code>{user_id}</code>\n"
        f"{ce(E_SUPPORT)} Username: @{user['username'] if user and user['username'] else '—'}\n"
        f"{ce(E_MONEY)} Amount: <b>{fmt_money(amount_paise)}</b>\n"
        f"{ce(E_WITHDRAW)} UPI ID: <code>{upi}</code>\n"
        f"{ce(E_STAR)} Request ID: <code>{wd_id}</code>\n"
        f"{ce(E_CALENDAR)} Requested: {_now()[:16].replace('T', ' ')}"
    )
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[[
        btn("APPROVE", callback_data=f"wd_approve:{wd_id}",
            style="success", emoji_index=E_APPROVE),
        btn("REJECT", callback_data=f"wd_reject:{wd_id}",
            style="danger", emoji_index=E_REJECT),
    ]])
    for admin_id in ADMIN_IDS:
        try:
            await safe_send(bot, admin_id, admin_text, reply_markup=admin_kb)
        except Exception as exc:
            log.warning("Failed to notify admin %s: %s", admin_id, exc)
    await safe_answer(callback, "Withdrawal request submitted!", alert=True)


# ─────────────────────────────────────────────────────────────────────────────
# 12. ADMIN WITHDRAWAL CALLBACKS
# ─────────────────────────────────────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


@router.callback_query(F.data.startswith("wd_approve:"))
async def cb_wd_approve(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await safe_answer(callback, "Unauthorized.", alert=True)
        return
    wd_id = callback.data.split(":", 1)[1]
    ok, status, wd = await asyncio.to_thread(approve_withdrawal,
                                             wd_id, callback.from_user.id)
    if not ok:
        await safe_answer(callback, f"Cannot approve: {status}.", alert=True)
        return
    await safe_edit(bot, callback.message.chat.id, callback.message.message_id,
        f"{ce(E_APPROVE)} <b>APPROVED</b>\n\n"
        f"{ce(E_STAR)} Request <code>{wd_id}</code> approved.\n"
        f"{ce(E_WARN)} Transfer the payment, then tap <b>MARK AS PAID</b>.",
        InlineKeyboardMarkup(inline_keyboard=[[
            btn("MARK AS PAID", callback_data=f"wd_paid:{wd_id}",
                style="success", emoji_index=E_MONEY)
        ]]))
    await safe_send(bot, wd["user_id"],
        f"{ce(E_APPROVE)} <b>Withdrawal approved</b>\n\n"
        f"{ce(E_STAR)} Request ID: <code>{wd_id}</code>\n"
        f"{ce(E_MONEY)} Amount: <b>{fmt_money(wd['amount_paise'])}</b>\n"
        f"{ce(E_WITHDRAW)} UPI: <code>{wd['upi_id']}</code>\n\n"
        f"{ce(E_WARN)} Your withdrawal has been approved and is awaiting payment.")
    await safe_answer(callback, "Approved. Mark as paid after transfer.")


@router.callback_query(F.data.startswith("wd_paid:"))
async def cb_wd_paid(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await safe_answer(callback, "Unauthorized.", alert=True)
        return
    wd_id = callback.data.split(":", 1)[1]
    ok, status, wd = await asyncio.to_thread(mark_withdrawal_paid,
                                             wd_id, callback.from_user.id)
    if not ok:
        await safe_answer(callback, f"Cannot mark paid: {status}.", alert=True)
        return
    await safe_edit(bot, callback.message.chat.id, callback.message.message_id,
        f"{ce(E_SUCCESS)} <b>PAID</b>\n\nRequest <code>{wd_id}</code> marked as paid.")
    await safe_send(bot, wd["user_id"],
        f"{ce(E_SUCCESS)} <b>Payment completed</b>\n\n"
        f"Your withdrawal has been approved.\n\n"
        f"The payment of <b>{fmt_money(wd['amount_paise'])}</b> has been sent to your "
        f"registered UPI ID: <code>{wd['upi_id']}</code>.\n\n"
        f"Thank you for using {BOT_NAME}.")
    await safe_answer(callback, "Marked as paid and user notified.")


@router.callback_query(F.data.startswith("wd_reject:"))
async def cb_wd_reject(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await safe_answer(callback, "Unauthorized.", alert=True)
        return
    wd_id = callback.data.split(":", 1)[1]
    ok, status, wd = await asyncio.to_thread(reject_withdrawal,
                                             wd_id, callback.from_user.id,
                                             "Rejected by admin")
    if not ok:
        await safe_answer(callback, f"Cannot reject: {status}.", alert=True)
        return
    await safe_edit(bot, callback.message.chat.id, callback.message.message_id,
        f"{ce(E_REJECT)} <b>REJECTED</b>\n\nRequest <code>{wd_id}</code> rejected and amount refunded.")
    await safe_send(bot, wd["user_id"],
        f"{ce(E_REJECT)} <b>Withdrawal rejected</b>\n\n"
        f"{ce(E_STAR)} Request ID: <code>{wd_id}</code>\n"
        f"{ce(E_MONEY)} Amount: <b>{fmt_money(wd['amount_paise'])}</b>\n\n"
        f"{ce(E_SUCCESS)} The amount has been returned to your available balance.")
    await safe_answer(callback, "Rejected and refunded.")


# ─────────────────────────────────────────────────────────────────────────────
# 13. ADMIN PANEL
# ─────────────────────────────────────────────────────────────────────────────

class PromoStates(StatesGroup):
    waiting_link = State()


class BroadcastStates(StatesGroup):
    waiting_message = State()


def admin_panel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("ADMIN DASHBOARD", callback_data="admin_dashboard", style="primary", emoji_index=E_ADMIN)],
        [btn("ALL USERS", callback_data="admin_all_users", style="success", emoji_index=E_USERS)],
        [btn("USER LIST", callback_data="admin_users:0", style="primary", emoji_index=E_SEARCH)],
        [btn("WITHDRAWAL REQUESTS", callback_data="admin_withdrawals", style="success", emoji_index=E_WITHDRAW)],
        [btn("ADD PROMO CHANNEL", callback_data="admin_add_promo", style="primary", emoji_index=E_ADD)],
        [btn("REMOVE PROMO CHANNEL", callback_data="admin_remove_promo", style="danger", emoji_index=E_REMOVE)],
        [btn("BROADCAST", callback_data="admin_broadcast", style="success", emoji_index=E_BROADCAST)],
        [btn("BOT STATISTICS", callback_data="admin_stats", style="primary", emoji_index=E_STATS)],
        [btn("CLOSE PANEL", callback_data="back_main", style="danger", emoji_index=E_CLOSE)],
    ])


@router.callback_query(F.data == "admin_panel")
async def cb_admin_panel(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await safe_answer(callback, "Unauthorized.", alert=True)
        return
    await safe_edit(bot, callback.message.chat.id, callback.message.message_id,
        f"{ce(E_ADMIN)} <b>ADMIN PANEL</b>\n\n"
        f"{ce(E_CROWN)} Authorized administrators:\n"
        + "\n".join(f"• {u}" for u in ADMIN_USERNAMES),
        admin_panel_kb())
    await safe_answer(callback, "Admin panel opened.")


@router.callback_query(F.data == "admin_dashboard")
async def cb_admin_dashboard(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await safe_answer(callback, "Unauthorized.", alert=True)
        return
    s = await asyncio.to_thread(admin_stats)
    text = (
        f"{ce(E_ADMIN)} <b>ADMIN DASHBOARD</b>\n\n"
        f"{ce(E_USERS)} Total users: <b>{s['total_users']}</b>\n"
        f"{ce(E_LINK)} Total referrals: <b>{s['total_referrals']}</b>\n"
        f"{ce(E_SUCCESS)} Successful referrals: <b>{s['successful_referrals']}</b>\n"
        f"{ce(E_MONEY)} Total referral rewards: <b>{fmt_money(s['total_referral_rewards'])}</b>\n\n"
        f"{ce(E_WITHDRAW)} Pending withdrawals: <b>{s['pending_withdrawals']}</b>\n"
        f"{ce(E_APPROVE)} Approved withdrawals: <b>{s['approved_withdrawals']}</b>\n"
        f"{ce(E_HISTORY)} Paid withdrawals: <b>{s['paid_withdrawals']}</b>\n\n"
        f"{ce(E_BALANCE)} Total available user balance: <b>{fmt_money(s['total_available_balance'])}</b>\n"
        f"{ce(E_WARN)} Total reserved balance: <b>{fmt_money(s['total_reserved_balance'])}</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("BACK", callback_data="admin_panel", style="danger", emoji_index=E_REMOVE)]
    ])
    await safe_edit(bot, callback.message.chat.id, callback.message.message_id, text, kb)


@router.callback_query(F.data == "admin_all_users")
async def cb_admin_all_users(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await safe_answer(callback, "Unauthorized.", alert=True)
        return
    total = await asyncio.to_thread(count_users)
    await safe_answer(callback, f"Total registered users: {total}", alert=True)


@router.callback_query(F.data.startswith("admin_users:"))
async def cb_admin_users_page(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await safe_answer(callback, "Unauthorized.", alert=True)
        return
    page = int(callback.data.split(":", 1)[1])
    users = await asyncio.to_thread(list_users_page, page)
    total = await asyncio.to_thread(count_users)
    lines = [f"{ce(E_USERS)} <b>USER LIST</b> (page {page + 1})\n"]
    if not users:
        lines.append("No users on this page.")
    for u in users:
        lines.append(
            f"{ce(E_SUPPORT)} <code>{u['user_id']}</code> — @{u['username'] or '—'}\n"
            f"   {ce(E_BALANCE)} {fmt_money(u['balance_paise'])} | "
            f"{ce(E_USERS)} refs: {u['successful_referrals']} | "
            f"{ce(E_CALENDAR)} {u['created_at'][:10]}"
        )
    nav = []
    if page > 0:
        nav.append(btn("PREV", callback_data=f"admin_users:{page - 1}",
                       style="primary", emoji_index=E_REMOVE))
    if (page + 1) * 10 < total:
        nav.append(btn("NEXT", callback_data=f"admin_users:{page + 1}",
                       style="success", emoji_index=E_ADD))
    rows = []
    if nav:
        rows.append(nav)
    rows.append([btn("BACK", callback_data="admin_panel",
                     style="danger", emoji_index=E_REMOVE)])
    await safe_edit(bot, callback.message.chat.id, callback.message.message_id,
                    "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "admin_withdrawals")
async def cb_admin_withdrawals(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await safe_answer(callback, "Unauthorized.", alert=True)
        return
    pending = await asyncio.to_thread(list_pending_withdrawals)
    if not pending:
        await safe_answer(callback, "No pending withdrawal requests.", alert=True)
        return
    for wd in pending:
        text = (
            f"{ce(E_WITHDRAW)} <b>PENDING WITHDRAWAL</b>\n\n"
            f"{ce(E_STAR)} ID: <code>{wd['id']}</code>\n"
            f"{ce(E_USERS)} User: <code>{wd['user_id']}</code>\n"
            f"{ce(E_MONEY)} Amount: <b>{fmt_money(wd['amount_paise'])}</b>\n"
            f"{ce(E_WITHDRAW)} UPI: <code>{wd['upi_id']}</code>\n"
            f"{ce(E_CALENDAR)} {wd['requested_at'][:16].replace('T', ' ')}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            btn("APPROVE", callback_data=f"wd_approve:{wd['id']}",
                style="success", emoji_index=E_APPROVE),
            btn("REJECT", callback_data=f"wd_reject:{wd['id']}",
                style="danger", emoji_index=E_REJECT),
        ]])
        await safe_send(bot, callback.message.chat.id, text, reply_markup=kb)
    await safe_answer(callback, f"{len(pending)} pending request(s).")


@router.callback_query(F.data == "admin_add_promo")
async def cb_admin_add_promo(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await safe_answer(callback, "Unauthorized.", alert=True)
        return
    await state.set_state(PromoStates.waiting_link)
    await safe_edit(bot, callback.message.chat.id, callback.message.message_id,
        f"{ce(E_ADD)} <b>ADD PROMO CHANNEL</b>\n\n"
        f"{ce(E_LINK)} Send the public channel link (e.g. <code>https://t.me/yourchannel</code>) "
        f"or the numeric ID (<code>-1001234567890</code>) for a private channel.\n\n"
        f"{ce(E_WARN)} The bot must be an administrator in the channel.",
        InlineKeyboardMarkup(inline_keyboard=[[
            btn("CANCEL", callback_data="admin_panel", style="danger", emoji_index=E_REMOVE)
        ]]))


@router.message(PromoStates.waiting_link)
async def promo_link_received(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if not raw:
        await safe_send(bot, message.chat.id, f"{ce(E_ERROR)} Send a valid link or numeric ID.")
        return
    try:
        if raw.lstrip("-").isdigit():
            chat_id = int(raw)
        elif raw.startswith("https://t.me/") or raw.startswith("@"):
            chat_id = "@" + raw.rstrip("/").split("/")[-1].lstrip("@")
        else:
            chat_id = "@" + raw.lstrip("@")
        chat = await bot.get_chat(chat_id)
        member = await bot.get_chat_member(chat.id, bot.id)
        if member.status not in ("administrator", "creator"):
            await safe_send(bot, message.chat.id,
                f"{ce(E_ERROR)} The bot is not an administrator in <b>{chat.title}</b>. "
                f"Add the bot as admin and try again.")
            await state.clear()
            return
        identifier = f"@{chat.username}" if chat.username else str(chat.id)
        await asyncio.to_thread(add_promo_channel, raw, identifier,
                                chat.title or str(chat.id), message.from_user.id)
        await state.clear()
        await safe_send(bot, message.chat.id,
            f"{ce(E_SUCCESS)} Promo channel added:\n"
            f"{ce(E_LINK)} <b>{chat.title}</b>\n"
            f"{ce(E_SUPPORT)} <code>{identifier}</code>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                btn("BACK", callback_data="admin_panel",
                    style="danger", emoji_index=E_REMOVE)
            ]]))
    except Exception as exc:
        log.warning("Promo channel add failed: %s", exc)
        await safe_send(bot, message.chat.id,
            f"{ce(E_ERROR)} Could not resolve this channel.\n\n"
            f"{ce(E_WARN)} For private channels, send the numeric ID "
            f"(e.g. <code>-1001234567890</code>) and make sure the bot is an administrator.\n\n"
            f"Details: <code>{_html_escape.escape(str(exc))}</code>")
        await state.clear()


@router.callback_query(F.data == "admin_remove_promo")
async def cb_admin_remove_promo(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await safe_answer(callback, "Unauthorized.", alert=True)
        return
    channels = await asyncio.to_thread(list_promo_channels)
    if not channels:
        await safe_answer(callback, "No promo channels configured.", alert=True)
        return
    rows = [
        [btn(f"REMOVE: {ch['title'] or ch['channel_link']}",
             callback_data=f"promo_remove_confirm:{ch['id']}",
             style="danger", emoji_index=E_REMOVE)]
        for ch in channels
    ]
    rows.append([btn("BACK", callback_data="admin_panel",
                     style="primary", emoji_index=E_REMOVE)])
    await safe_edit(bot, callback.message.chat.id, callback.message.message_id,
        f"{ce(E_REMOVE)} <b>REMOVE PROMO CHANNEL</b>\n\nSelect a channel to remove:",
        InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("promo_remove_confirm:"))
async def cb_promo_remove_confirm(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await safe_answer(callback, "Unauthorized.", alert=True)
        return
    ch_id = int(callback.data.split(":", 1)[1])
    await asyncio.to_thread(remove_promo_channel, ch_id)
    await safe_answer(callback, "Promo channel removed.", alert=True)
    await safe_edit(bot, callback.message.chat.id, callback.message.message_id,
        f"{ce(E_SUCCESS)} Promo channel removed.",
        InlineKeyboardMarkup(inline_keyboard=[[
            btn("BACK", callback_data="admin_panel",
                style="danger", emoji_index=E_REMOVE)
        ]]))


@router.callback_query(F.data == "admin_broadcast")
async def cb_admin_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await safe_answer(callback, "Unauthorized.", alert=True)
        return
    await state.set_state(BroadcastStates.waiting_message)
    await safe_edit(bot, callback.message.chat.id, callback.message.message_id,
        f"{ce(E_BROADCAST)} <b>BROADCAST</b>\n\n"
        f"{ce(E_STAR)} Send the message you want to broadcast (text, photo with caption, etc.).\n"
        f"{ce(E_WARN)} It will be copied to every registered user.",
        InlineKeyboardMarkup(inline_keyboard=[[
            btn("CANCEL", callback_data="admin_panel",
                style="danger", emoji_index=E_REMOVE)
        ]]))


@router.message(BroadcastStates.waiting_message)
async def broadcast_message_received(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    await safe_send(bot, message.chat.id, f"{ce(E_BROADCAST)} Broadcast started…")
    with db() as c:
        users = c.execute("SELECT user_id FROM users").fetchall()
    success = 0
    failed = 0
    for row in users:
        uid = row["user_id"]
        try:
            await bot.copy_message(chat_id=uid, from_chat_id=message.chat.id,
                                   message_id=message.message_id)
            success += 1
        except Exception as exc:
            failed += 1
            log.debug("Broadcast failed for %s: %s", uid, exc)
        await asyncio.sleep(0.05)
    await safe_send(bot, message.chat.id,
        f"{ce(E_SUCCESS)} <b>Broadcast finished</b>\n\n"
        f"{ce(E_USERS)} Recipients: {len(users)}\n"
        f"{ce(E_APPROVE)} Delivered: {success}\n"
        f"{ce(E_REJECT)} Failed: {failed}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            btn("BACK", callback_data="admin_panel",
                style="danger", emoji_index=E_REMOVE)
        ]]))


@router.callback_query(F.data == "admin_stats")
async def cb_admin_stats(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await safe_answer(callback, "Unauthorized.", alert=True)
        return
    s = await asyncio.to_thread(admin_stats)
    text = (
        f"{ce(E_STATS)} <b>BOT STATISTICS</b>\n\n"
        f"{ce(E_USERS)} Total users: <b>{s['total_users']}</b>\n"
        f"{ce(E_LINK)} Total referrals: <b>{s['total_referrals']}</b>\n"
        f"{ce(E_SUCCESS)} Successful referrals: <b>{s['successful_referrals']}</b>\n"
        f"{ce(E_MONEY)} Total referral rewards: <b>{fmt_money(s['total_referral_rewards'])}</b>\n"
        f"{ce(E_WITHDRAW)} Pending withdrawals: <b>{s['pending_withdrawals']}</b>\n"
        f"{ce(E_APPROVE)} Approved withdrawals: <b>{s['approved_withdrawals']}</b>\n"
        f"{ce(E_HISTORY)} Paid withdrawals: <b>{s['paid_withdrawals']}</b>\n"
        f"{ce(E_BALANCE)} Available user balance: <b>{fmt_money(s['total_available_balance'])}</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("BACK", callback_data="admin_panel",
             style="danger", emoji_index=E_REMOVE)]
    ])
    await safe_edit(bot, callback.message.chat.id, callback.message.message_id, text, kb)


# ─────────────────────────────────────────────────────────────────────────────
# 14. FLASK WEBSITE + WEBHOOK
# ─────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)

_loop: Optional[asyncio.AbstractEventLoop] = None
_loop_ready = threading.Event()
_loop_started = False
_loop_lock = threading.Lock()


async def _startup_channel_check() -> None:
    try:
        me = await bot.me()
        member = await bot.get_chat_member(MAIN_CHANNEL_USERNAME, me.id)
        if member.status in ("administrator", "creator"):
            log.info("✅ Bot is admin in %s", MAIN_CHANNEL_USERNAME)
        else:
            log.warning(
                "⚠️ Bot is NOT admin in %s (status=%s). "
                "Force-join will fail — add the bot as Administrator!",
                MAIN_CHANNEL_USERNAME, member.status)
    except Exception as exc:
        log.warning(
            "⚠️ Cannot verify bot membership in %s: %s. "
            "Add the bot as administrator in that channel!",
            MAIN_CHANNEL_USERNAME, exc)


async def _set_webhook() -> None:
    try:
        me = await bot.me()
        global _bot_username_cache
        _bot_username_cache = me.username
        log.info("Bot authenticated as @%s (id=%s)", me.username, me.id)
    except Exception as exc:
        log.exception("Bot authentication FAILED — check BOT_TOKEN: %s", exc)
        return
    if not WEBHOOK_URL or not WEBHOOK_SECRET:
        log.error("WEBHOOK_URL or WEBHOOK_SECRET missing; webhook not set.")
        return
    url = f"{WEBHOOK_URL}/webhook/{WEBHOOK_SECRET}"
    try:
        await bot.set_webhook(url=url, secret_token=WEBHOOK_SECRET,
                              drop_pending_updates=True,
                              allowed_updates=["message", "callback_query"])
        log.info("Webhook set to %s", url)
    except Exception as exc:
        log.exception("Failed to set webhook: %s", exc)


def _run_background_loop() -> None:
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    try:
        _loop.run_until_complete(_set_webhook())
        _loop.run_until_complete(_startup_channel_check())
    except Exception as exc:
        log.exception("Background init error: %s", exc)
    finally:
        _loop_ready.set()
    _loop.run_forever()


def ensure_background_loop() -> None:
    global _loop_started
    with _loop_lock:
        if _loop_started:
            return
        t = threading.Thread(target=_run_background_loop,
                             daemon=True, name="aiogram-loop")
        t.start()
        if not _loop_ready.wait(15):
            log.error("Background bot loop did not become ready in time.")
        _loop_started = True


@app.before_request
def _ensure_bot() -> None:
    ensure_background_loop()


@app.route("/webhook/<secret>", methods=["POST"])
def telegram_webhook(secret: str):
    if secret != WEBHOOK_SECRET:
        return jsonify({"ok": False, "error": "forbidden"}), 403
    header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if header_secret != WEBHOOK_SECRET:
        return jsonify({"ok": False, "error": "invalid secret header"}), 403
    ensure_background_loop()
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"ok": False, "error": "bad request"}), 400
    try:
        update = Update.model_validate(data)
    except Exception as exc:
        log.warning("Invalid update payload: %s", exc)
        return jsonify({"ok": False, "error": "invalid update"}), 400
    if not _loop or not _loop.is_running():
        log.error("Event loop is not running.")
        return jsonify({"ok": False, "error": "bot not ready"}), 503
    future = asyncio.run_coroutine_threadsafe(
        dp.feed_webhook_update(bot, update), _loop)
    def _log_exception(f):
        try:
            exc = f.exception()
            if exc:
                log.error("Update processing failed: %s", exc, exc_info=exc)
        except Exception:
            pass
    future.add_done_callback(_log_exception)
    return jsonify({"ok": True}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "bot": BOT_NAME,
                    "visits": get_visit_count()})


WEBSITE_HTML = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CYTUN REFER BOT</title>
<style>
  :root{--bg:#050816;--card:#0d1226;--line:rgba(255,255,255,.08);
        --text:#eef2ff;--muted:#94a3b8;}
  *{box-sizing:border-box;margin:0;padding:0}
  body{min-height:100vh;display:flex;align-items:center;justify-content:center;
       background:radial-gradient(circle at 20% 20%,#1e1b4b 0%,var(--bg) 45%,#020617 100%);
       color:var(--text);font-family:Inter,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
       padding:24px;}
  .card{width:100%;max-width:760px;padding:44px 36px;border-radius:28px;
        background:linear-gradient(160deg,rgba(255,255,255,.07),rgba(255,255,255,.02));
        border:1px solid var(--line);backdrop-filter:blur(18px);
        box-shadow:0 30px 80px rgba(0,0,0,.55);text-align:center;}
  .badge{display:inline-flex;gap:8px;align-items:center;padding:8px 14px;
         border-radius:999px;border:1px solid var(--line);color:var(--muted);
         font-size:12px;letter-spacing:.12em;text-transform:uppercase;}
  h1{margin:22px 0 10px;font-size:clamp(28px,5vw,48px);line-height:1.1;
     background:linear-gradient(90deg,#a78bfa,#22d3ee,#22c55e);
     -webkit-background-clip:text;background-clip:text;color:transparent;}
  p{color:var(--muted);line-height:1.7;font-size:16px;max-width:560px;margin:0 auto}
  .stats{display:grid;grid-template-columns:repeat(2,1fr);gap:14px;margin:30px 0 26px;}
  .stat{padding:20px 16px;border-radius:18px;border:1px solid var(--line);
        background:rgba(255,255,255,.03);}
  .stat .num{font-size:30px;font-weight:800;color:#fff}
  .stat .lbl{font-size:12px;color:var(--muted);letter-spacing:.08em;
             text-transform:uppercase;margin-top:6px}
  .cta{display:inline-flex;align-items:center;justify-content:center;gap:10px;
       width:100%;padding:16px 22px;border-radius:16px;text-decoration:none;
       font-weight:700;letter-spacing:.04em;color:#04110a;
       background:linear-gradient(135deg,#34d399,#22c55e);
       box-shadow:0 18px 40px rgba(34,197,94,.3);}
  .cta:hover{filter:brightness(1.08)}
  .foot{margin-top:18px;font-size:12px;color:#64748b}
  @media (max-width:520px){.card{padding:30px 20px}.stats{grid-template-columns:1fr}}
</style>
</head>
<body>
  <main class="card">
    <span class="badge">✨ Premium Referral Platform</span>
    <h1>CYTUN REFER BOT</h1>
    <p>Earn ₹2 for every successful referral. Join the bot, share your link, and withdraw once you reach ₹10.</p>
    <section class="stats">
      <div class="stat"><div class="num">{{ visits }}</div><div class="lbl">Website Visits</div></div>
      <div class="stat"><div class="num">₹2</div><div class="lbl">Per Referral</div></div>
    </section>
    <a class="cta" href="{{ bot_link }}" target="_blank" rel="noopener">🚀 OPEN TELEGRAM BOT</a>
    <p class="foot">Secure • Fast • Premium</p>
  </main>
</body>
</html>
"""


@app.route("/", methods=["GET"])
def index():
    seen = request.cookies.get("xyven_seen")
    visits = get_visit_count() if seen else increment_visit()
    bot_link = f"https://t.me/{_bot_username_cache or BOT_USERNAME or 'YOUR_BOT'}"
    html = render_template_string(WEBSITE_HTML, visits=visits, bot_link=bot_link)
    resp = make_response(html)
    if not seen:
        resp.set_cookie("xyven_seen", "1", max_age=60 * 60 * 6,
                        httponly=True, samesite="Lax", secure=request.is_secure)
    return resp


# ─────────────────────────────────────────────────────────────────────────────
# 15. STARTUP
# ─────────────────────────────────────────────────────────────────────────────

_ensure_db_dir()
init_db()

if __name__ == "__main__":
    ensure_background_loop()
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
