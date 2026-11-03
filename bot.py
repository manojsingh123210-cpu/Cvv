"""
CYTUN REFER BOT
Single-file production bot: aiogram 3.28.2 + Flask + SQLite.

NEW IN THIS VERSION:
  - Join request → auto-approve → auto-verify → push dashboard immediately
  - No more "tap VERIFY JOIN" friction for private channels
  - Aesthetic text: blockquotes, dividers, monospace amounts
  - Personalised welcome with user first name
  - All previous fixes retained (DB fallback, bot.me, 4-level safe_send,
    webhook header validation, cmd_start crash-proof)

Run with ONE worker:
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
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    ChatJoinRequest,
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
# 1. CONFIG
# ─────────────────────────────────────────────────────────────────────────────

BOT_NAME = os.getenv("BOT_NAME", "CYTUN REFER BOT").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip().lstrip("@")

ADMIN_USERNAMES = ["@cytunn", "@R0syug"]
ADMIN_IDS: set[int] = set()
for part in os.getenv("ADMIN_IDS", "").replace(" ", "").split(","):
    if part.isdigit():
        ADMIN_IDS.add(int(part))

MAIN_CHANNEL = "https://t.me/whymeyarrr"
MAIN_CHANNEL_USERNAME = "@whymeyarrr"

REFERRAL_REWARD_PAISE = 200
MIN_WITHDRAWAL_PAISE = 1000

DATABASE_PATH = os.getenv("DATABASE_PATH", "xyven.db")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
WEBSITE_URL = os.getenv("WEBSITE_URL", "").rstrip("/")

DIVIDER = "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬"
DIVIDER_THIN = "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄"

# ─────────────────────────────────────────────────────────────────────────────
# 2. CUSTOM EMOJIS
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


def ce(i: int, fallback: Optional[str] = None) -> str:
    eid = EMOJI_IDS[i % len(EMOJI_IDS)]
    fb = fallback or FALLBACK_EMOJI[i % len(FALLBACK_EMOJI)]
    return f'<tg-emoji emoji-id="{eid}">{fb}</tg-emoji>'


# ─────────────────────────────────────────────────────────────────────────────
# 3. LOGGING
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
log = logging.getLogger("cytun")


# ─────────────────────────────────────────────────────────────────────────────
# 4. DB PATH GUARD
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_db_dir() -> None:
    global DATABASE_PATH
    d = os.path.dirname(os.path.abspath(DATABASE_PATH)) or "."
    try:
        os.makedirs(d, exist_ok=True)
        test = os.path.join(d, ".write_test")
        with open(test, "w") as f: f.write("ok")
        os.remove(test)
        log.info("Database directory OK: %s", d)
    except (PermissionError, OSError) as exc:
        fb = "/tmp/cytun.db"
        log.warning("DB dir %s not writable (%s). Fallback -> %s", d, exc, fb)
        DATABASE_PATH = fb


# ─────────────────────────────────────────────────────────────────────────────
# 5. DB LAYER
# ─────────────────────────────────────────────────────────────────────────────

_db_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    c = sqlite3.connect(DATABASE_PATH, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA busy_timeout=30000")
    return c


@contextmanager
def db(write: bool = False):
    if write: _db_lock.acquire()
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()
        if write: _db_lock.release()


def init_db() -> None:
    with db(write=True) as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY, username TEXT,
                first_name TEXT, last_name TEXT, referred_by INTEGER,
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
                id INTEGER PRIMARY KEY AUTOINCREMENT, referrer_id INTEGER NOT NULL,
                referred_id INTEGER NOT NULL UNIQUE, reward_paise INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'credited', created_at TEXT NOT NULL,
                FOREIGN KEY (referrer_id) REFERENCES users(user_id),
                FOREIGN KEY (referred_id) REFERENCES users(user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id);
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                type TEXT NOT NULL, amount_paise INTEGER NOT NULL,
                description TEXT, reference_id TEXT, created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_tx_user ON transactions(user_id, id DESC);
            CREATE TABLE IF NOT EXISTS withdrawals (
                id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                amount_paise INTEGER NOT NULL, upi_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', requested_at TEXT NOT NULL,
                processed_at TEXT, processed_by INTEGER, rejection_reason TEXT,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_wd_status ON withdrawals(status, requested_at);
            CREATE TABLE IF NOT EXISTS promo_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT, channel_link TEXT NOT NULL,
                chat_identifier TEXT NOT NULL, title TEXT, added_by INTEGER,
                added_at TEXT NOT NULL, invite_link TEXT
            );
            CREATE TABLE IF NOT EXISTS website_visits (
                id INTEGER PRIMARY KEY CHECK (id = 1), count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY, added_at TEXT NOT NULL);
        """)
        c.execute("INSERT OR IGNORE INTO website_visits (id, count) VALUES (1, 0)")
        c.commit()


def _migrate_db() -> None:
    with db(write=True) as c:
        try:
            cols = [r[1] for r in c.execute("PRAGMA table_info(promo_channels)").fetchall()]
            if "invite_link" not in cols:
                c.execute("ALTER TABLE promo_channels ADD COLUMN invite_link TEXT")
                log.info("Migration: added invite_link")
            c.commit()
        except Exception as exc:
            log.error("Migration failed: %s", exc)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fmt_money(paise: int) -> str:
    return f"₹{paise / 100:,.2f}"


def get_user(uid: int) -> Optional[sqlite3.Row]:
    with db() as c:
        return c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()


def register_user(uid, uname, fname, lname, ref_by) -> tuple[bool, str]:
    with db(write=True) as c:
        ex = c.execute("SELECT is_verified FROM users WHERE user_id=?", (uid,)).fetchone()
        if ex: return bool(ex["is_verified"]), "exists"
        c.execute(
            """INSERT INTO users (user_id, username, first_name, last_name,
               referred_by, is_verified, created_at) VALUES (?,?,?,?,?,0,?)""",
            (uid, uname, fname, lname, ref_by, _now()))
        if ref_by and ref_by != uid:
            c.execute("UPDATE users SET total_referrals=total_referrals+1 WHERE user_id=?", (ref_by,))
        c.commit()
        return False, "created"


def mark_verified_and_credit(uid: int) -> tuple[bool, Optional[int], int]:
    with db(write=True) as c:
        row = c.execute("SELECT referred_by, is_verified FROM users WHERE user_id=?", (uid,)).fetchone()
        if not row or row["is_verified"]:
            return False, None, 0
        c.execute("UPDATE users SET is_verified=1 WHERE user_id=?", (uid,))
        ref = row["referred_by"]; reward = 0
        if ref and ref != uid:
            r = c.execute("SELECT user_id FROM users WHERE user_id=?", (ref,)).fetchone()
            a = c.execute("SELECT id FROM referrals WHERE referred_id=?", (uid,)).fetchone()
            if r and not a:
                c.execute("INSERT INTO referrals (referrer_id, referred_id, reward_paise, status, created_at) VALUES (?,?,?,'credited',?)",
                          (ref, uid, REFERRAL_REWARD_PAISE, _now()))
                c.execute("""UPDATE users SET balance_paise=balance_paise+?,
                    total_earned_paise=total_earned_paise+?,
                    successful_referrals=successful_referrals+1 WHERE user_id=?""",
                          (REFERRAL_REWARD_PAISE, REFERRAL_REWARD_PAISE, ref))
                c.execute("INSERT INTO transactions (user_id, type, amount_paise, description, created_at) VALUES (?,?,?,?,?)",
                          (ref, "referral_reward", REFERRAL_REWARD_PAISE,
                           f"Referral reward for {uid}", _now()))
                reward = REFERRAL_REWARD_PAISE
        c.commit()
        return True, ref, reward


def list_promo_channels() -> list[sqlite3.Row]:
    with db() as c:
        return c.execute("SELECT * FROM promo_channels ORDER BY id").fetchall()


def add_or_update_promo_channel(link, ident, title, by, invite=None) -> None:
    with db(write=True) as c:
        ex = c.execute("SELECT id FROM promo_channels WHERE chat_identifier=?", (ident,)).fetchone()
        if ex:
            c.execute("UPDATE promo_channels SET channel_link=?, title=?, invite_link=? WHERE chat_identifier=?",
                      (link, title, invite, ident))
        else:
            c.execute("""INSERT INTO promo_channels (channel_link, chat_identifier,
                title, added_by, added_at, invite_link) VALUES (?,?,?,?,?,?)""",
                      (link, ident, title, by, _now(), invite))
        c.commit()


def remove_promo_channel(cid: int) -> None:
    with db(write=True) as c:
        c.execute("DELETE FROM promo_channels WHERE id=?", (cid,)); c.commit()


def update_promo_invite_link(ident: str, invite: str) -> None:
    with db(write=True) as c:
        c.execute("UPDATE promo_channels SET invite_link=? WHERE chat_identifier=?", (invite, ident))
        c.commit()


def increment_visit() -> int:
    with db(write=True) as c:
        c.execute("UPDATE website_visits SET count = count + 1 WHERE id = 1"); c.commit()
        r = c.execute("SELECT count FROM website_visits WHERE id=1").fetchone()
        return int(r["count"]) if r else 0


def get_visit_count() -> int:
    with db() as c:
        r = c.execute("SELECT count FROM website_visits WHERE id=1").fetchone()
        return int(r["count"]) if r else 0


def create_withdrawal(wid, uid, amt, upi) -> tuple[bool, str]:
    with db(write=True) as c:
        u = c.execute("SELECT balance_paise FROM users WHERE user_id=?", (uid,)).fetchone()
        if not u: return False, "no_user"
        if u["balance_paise"] < amt: return False, "insufficient"
        c.execute("UPDATE users SET balance_paise=balance_paise-?, reserved_paise=reserved_paise+? WHERE user_id=?",
                  (amt, amt, uid))
        c.execute("INSERT INTO withdrawals (id, user_id, amount_paise, upi_id, status, requested_at) VALUES (?,?,?,?,'pending',?)",
                  (wid, uid, amt, upi, _now()))
        c.execute("INSERT INTO transactions (user_id, type, amount_paise, description, reference_id, created_at) VALUES (?,?,?,?,?,?)",
                  (uid, "withdrawal_hold", -amt, f"Withdrawal {wid}", wid, _now()))
        c.commit()
        return True, "ok"


def approve_withdrawal(wid, aid) -> tuple[bool, str, Optional[sqlite3.Row]]:
    with db(write=True) as c:
        wd = c.execute("SELECT * FROM withdrawals WHERE id=?", (wid,)).fetchone()
        if not wd: return False, "not_found", None
        if wd["status"] != "pending": return False, f"already_{wd['status']}", wd
        c.execute("UPDATE withdrawals SET status='approved', processed_at=?, processed_by=? WHERE id=? AND status='pending'",
                  (_now(), aid, wid))
        c.commit()
        return True, "approved", c.execute("SELECT * FROM withdrawals WHERE id=?", (wid,)).fetchone()


def mark_withdrawal_paid(wid, aid) -> tuple[bool, str, Optional[sqlite3.Row]]:
    with db(write=True) as c:
        wd = c.execute("SELECT * FROM withdrawals WHERE id=?", (wid,)).fetchone()
        if not wd: return False, "not_found", None
        if wd["status"] == "paid": return False, "already_paid", wd
        if wd["status"] != "approved": return False, f"not_approved_{wd['status']}", wd
        c.execute("UPDATE withdrawals SET status='paid', processed_at=?, processed_by=? WHERE id=? AND status='approved'",
                  (_now(), aid, wid))
        c.execute("UPDATE users SET reserved_paise=reserved_paise-?, total_withdrawn_paise=total_withdrawn_paise+? WHERE user_id=?",
                  (wd["amount_paise"], wd["amount_paise"], wd["user_id"]))
        c.execute("INSERT INTO transactions (user_id, type, amount_paise, description, reference_id, created_at) VALUES (?,?,?,?,?,?)",
                  (wd["user_id"], "withdrawal_paid", 0, f"Withdrawal {wid} paid", wid, _now()))
        c.commit()
        return True, "paid", c.execute("SELECT * FROM withdrawals WHERE id=?", (wid,)).fetchone()


def reject_withdrawal(wid, aid, reason="") -> tuple[bool, str, Optional[sqlite3.Row]]:
    with db(write=True) as c:
        wd = c.execute("SELECT * FROM withdrawals WHERE id=?", (wid,)).fetchone()
        if not wd: return False, "not_found", None
        if wd["status"] != "pending": return False, f"already_{wd['status']}", wd
        c.execute("""UPDATE withdrawals SET status='rejected', processed_at=?,
            processed_by=?, rejection_reason=? WHERE id=? AND status='pending'""",
                  (_now(), aid, reason or "Rejected", wid))
        c.execute("UPDATE users SET balance_paise=balance_paise+?, reserved_paise=reserved_paise-? WHERE user_id=?",
                  (wd["amount_paise"], wd["amount_paise"], wd["user_id"]))
        c.execute("INSERT INTO transactions (user_id, type, amount_paise, description, reference_id, created_at) VALUES (?,?,?,?,?,?)",
                  (wd["user_id"], "withdrawal_refund", wd["amount_paise"],
                   f"Withdrawal {wid} refunded", wid, _now()))
        c.commit()
        return True, "rejected", c.execute("SELECT * FROM withdrawals WHERE id=?", (wid,)).fetchone()


def list_pending_withdrawals(limit=10) -> list[sqlite3.Row]:
    with db() as c:
        return c.execute("SELECT * FROM withdrawals WHERE status='pending' ORDER BY requested_at DESC LIMIT ?",
                         (limit,)).fetchall()


def user_transactions(uid, limit=20) -> list[sqlite3.Row]:
    with db() as c:
        return c.execute("SELECT * FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT ?",
                         (uid, limit)).fetchall()


def admin_stats() -> dict[str, Any]:
    with db() as c:
        r = c.execute("""SELECT
            (SELECT COUNT(*) FROM users) AS total_users,
            (SELECT COUNT(*) FROM referrals) AS total_referrals,
            (SELECT COUNT(*) FROM referrals WHERE status='credited') AS successful_referrals,
            (SELECT COALESCE(SUM(reward_paise),0) FROM referrals WHERE status='credited') AS total_referral_rewards,
            (SELECT COUNT(*) FROM withdrawals WHERE status='pending') AS pending_withdrawals,
            (SELECT COUNT(*) FROM withdrawals WHERE status='approved') AS approved_withdrawals,
            (SELECT COUNT(*) FROM withdrawals WHERE status='paid') AS paid_withdrawals,
            (SELECT COALESCE(SUM(balance_paise),0) FROM users) AS total_available_balance,
            (SELECT COALESCE(SUM(reserved_paise),0) FROM users) AS total_reserved_balance""").fetchone()
        return dict(r)


def list_users_page(page, per=10) -> list[sqlite3.Row]:
    with db() as c:
        return c.execute("SELECT * FROM users ORDER BY created_at DESC LIMIT ? OFFSET ?",
                         (per, max(0, page) * per)).fetchall()


def count_users() -> int:
    with db() as c:
        return int(c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"])


# ─────────────────────────────────────────────────────────────────────────────
# 6. BUTTONS / SAFE SEND
# ─────────────────────────────────────────────────────────────────────────────

def btn(text, cb=None, *, style=None, emoji_index=None, url=None, copy_text=None) -> InlineKeyboardButton:
    kw: dict[str, Any] = {"text": text}
    if cb is not None: kw["callback_data"] = cb
    if url is not None: kw["url"] = url
    if copy_text is not None: kw["copy_text"] = CopyTextButton(text=copy_text)
    if style: kw["style"] = style
    if emoji_index is not None: kw["icon_custom_emoji_id"] = EMOJI_IDS[emoji_index % len(EMOJI_IDS)]
    return InlineKeyboardButton(**kw)


def strip_button_icons(m) -> Optional[InlineKeyboardMarkup]:
    if not m or not isinstance(m, InlineKeyboardMarkup): return m
    return InlineKeyboardMarkup(inline_keyboard=[
        [b.model_copy(update={"icon_custom_emoji_id": None}) for b in row] for row in m.inline_keyboard
    ])


_CUSTOM_RE = re.compile(r"<tg-emoji[^>]*>(.*?)</tg-emoji>", re.DOTALL)
_HTML_RE = re.compile(r"<[^>]+>")


def strip_custom_emoji(t: str) -> str: return _CUSTOM_RE.sub(r"\1", t)
def strip_all_html(t: str) -> str: return _HTML_RE.sub("", t)


async def safe_send(bot: Bot, chat_id: int, text: str,
                    reply_markup: Optional[InlineKeyboardMarkup] = None, **kw) -> Optional[Message]:
    try:
        return await bot.send_message(chat_id, text, reply_markup=reply_markup, **kw)
    except TelegramBadRequest as e1:
        log.warning("safe_send#1 %s: %s", chat_id, e1)
    try:
        return await bot.send_message(chat_id, strip_custom_emoji(text),
                                      reply_markup=strip_button_icons(reply_markup), **kw)
    except TelegramBadRequest as e2:
        log.warning("safe_send#2 %s: %s", chat_id, e2)
    try:
        return await bot.send_message(chat_id, strip_all_html(strip_custom_emoji(text)),
                                      reply_markup=strip_button_icons(reply_markup),
                                      parse_mode=None, **kw)
    except TelegramBadRequest as e3:
        log.warning("safe_send#3 %s: %s", chat_id, e3)
    try:
        return await bot.send_message(chat_id, strip_all_html(strip_custom_emoji(text)),
                                      parse_mode=None, **kw)
    except Exception as e4:
        log.exception("safe_send FAILED %s: %s", chat_id, e4)
        return None


async def safe_edit(bot: Bot, chat_id: int, mid: int, text: str,
                    reply_markup: Optional[InlineKeyboardMarkup] = None, **kw) -> Optional[Message]:
    try:
        return await bot.edit_message_text(text=text, chat_id=chat_id, message_id=mid,
                                           reply_markup=reply_markup, **kw)
    except TelegramBadRequest as e1:
        if "message is not modified" in str(e1).lower(): return None
        log.warning("safe_edit#1: %s", e1)
    try:
        return await bot.edit_message_text(text=strip_custom_emoji(text), chat_id=chat_id,
                                           message_id=mid, reply_markup=strip_button_icons(reply_markup), **kw)
    except TelegramBadRequest as e2:
        log.warning("safe_edit#2: %s", e2)
    try:
        return await bot.edit_message_text(text=strip_all_html(strip_custom_emoji(text)),
                                           chat_id=chat_id, message_id=mid,
                                           reply_markup=strip_button_icons(reply_markup),
                                           parse_mode=None, **kw)
    except Exception as e3:
        log.warning("safe_edit failed: %s", e3)
        return None


async def safe_answer(cb: CallbackQuery, text: str, alert: bool = False) -> None:
    try: await cb.answer(text, show_alert=alert)
    except TelegramBadRequest:
        try: await cb.answer()
        except: pass


# ─────────────────────────────────────────────────────────────────────────────
# 7. BOT / DISPATCHER
# ─────────────────────────────────────────────────────────────────────────────

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

router = Router()
dp = Dispatcher(storage=MemoryStorage())
dp.include_router(router)
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
_bot_username_cache: Optional[str] = BOT_USERNAME or None


def referral_link(uid: int) -> str:
    return f"https://t.me/{_bot_username_cache or BOT_USERNAME or 'YOUR_BOT'}?start={uid}"


# ─────────────────────────────────────────────────────────────────────────────
# 8. FORCE JOIN + INVITE LINK HELPERS
# ─────────────────────────────────────────────────────────────────────────────

async def _ensure_invite_link(chat_id: str, stored: Optional[str]) -> Optional[str]:
    if stored: return stored
    if chat_id.startswith("@"): return f"https://t.me/{chat_id.lstrip('@')}"
    try:
        obj = await bot.create_chat_invite_link(
            chat_id=int(chat_id), creates_join_request=True,
            name=f"CytunBot_{int(time.time())}")
        await asyncio.to_thread(update_promo_invite_link, chat_id, obj.invite_link)
        return obj.invite_link
    except Exception as exc:
        log.error("Invite link failed for %s: %s", chat_id, exc)
        return None


async def check_force_join(uid: int) -> tuple[list[dict], list[str]]:
    required = [{"title": "Main Channel", "chat_id": MAIN_CHANNEL_USERNAME,
                 "invite_link": MAIN_CHANNEL}]
    for ch in list_promo_channels():
        required.append({
            "title": ch["title"] or ch["channel_link"],
            "chat_id": ch["chat_identifier"],
            "invite_link": ch["invite_link"] if "invite_link" in ch.keys() else None,
        })
    missing, errors = [], []
    for req in required:
        try:
            m = await bot.get_chat_member(chat_id=req["chat_id"], user_id=uid)
            if m.status in ("left", "kicked"):
                req["invite_link"] = await _ensure_invite_link(
                    req["chat_id"], req.get("invite_link"))
                missing.append(req)
        except TelegramBadRequest as exc:
            errors.append(f"{req['title']}: {exc.message}")
    return missing, errors


def force_join_kb(missing: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for req in missing:
        link = req.get("invite_link")
        title = req['title'].upper()[:24]
        if link:
            rows.append([btn(f"JOIN  ›  {title}", url=link, style="primary",
                             emoji_index=E_LINK)])
        else:
            rows.append([btn(f"JOIN  ›  {title}", cb="support",
                             style="danger", emoji_index=E_WARN)])
    rows.append([btn("VERIFY  JOIN", cb="verify_join",
                     style="success", emoji_index=E_APPROVE)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ─────────────────────────────────────────────────────────────────────────────
# 9. AESTHETIC TEXT BUILDERS
# ─────────────────────────────────────────────────────────────────────────────

def welcome_text(first_name: str = "") -> str:
    name = f"<b>{_html_escape.escape(first_name)}</b>" if first_name else "friend"
    return (
        f"{ce(E_CROWN)}  <b>{BOT_NAME}</b>\n"
        f"{DIVIDER}\n\n"
        f"{ce(E_WELCOME)}  <i>Welcome aboard, {name}!</i>\n\n"
        f"<blockquote>{ce(E_MONEY)}  <b>₹2.00</b> per successful referral\n"
        f"{ce(E_USERS)}  Invite unlimited friends\n"
        f"{ce(E_WITHDRAW)}  Withdraw starting at <b>₹10.00</b></blockquote>\n\n"
        f"{ce(E_GIFT)}  <i>Pick an option below to start earning.</i>"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 10. MAIN MENU
# ─────────────────────────────────────────────────────────────────────────────

def main_menu_kb(uid: int) -> InlineKeyboardMarkup:
    rows = [
        [btn("MY ACCOUNT", cb="account", style="primary", emoji_index=E_STAR)],
        [btn("REFER & EARN", cb="refer", style="success", emoji_index=E_USERS)],
        [btn("MY BALANCE", cb="balance", style="success", emoji_index=E_BALANCE)],
        [btn("WITHDRAW", cb="withdraw", style="primary", emoji_index=E_WITHDRAW),
         btn("STATISTICS", cb="stats", style="success", emoji_index=E_STATS)],
        [btn("HOW TO EARN", cb="how_to_earn", style="primary", emoji_index=E_HISTORY),
         btn("SUPPORT", cb="support", style="success", emoji_index=E_SUPPORT)],
    ]
    if uid in ADMIN_IDS:
        rows.append([btn("ADMIN PANEL", cb="admin_panel", style="danger", emoji_index=E_ADMIN)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_main_menu(chat_id: int, uid: int, edit_id: Optional[int] = None,
                         custom_text: Optional[str] = None) -> None:
    user = await asyncio.to_thread(get_user, uid)
    name = (user["first_name"] or "") if user else ""
    text = custom_text or welcome_text(name)
    if edit_id:
        await safe_edit(bot, chat_id, edit_id, text, reply_markup=main_menu_kb(uid))
    else:
        await safe_send(bot, chat_id, text, reply_markup=main_menu_kb(uid))


# ─────────────────────────────────────────────────────────────────────────────
# 11. JOIN REQUEST → AUTO-VERIFY → PUSH DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────

@router.chat_join_request()
async def handle_chat_join_request(request: ChatJoinRequest) -> None:
    """
    When a user sends a join request to a promo channel:
      1. Approve automatically
      2. Re-check ALL required channels
      3. If all joined → verify + show dashboard immediately (no button needed)
      4. If not all joined → show progress + remaining JOIN buttons
    """
    uid = request.from_user.id
    fname = request.from_user.first_name or ""
    chat_title = request.chat.title or str(request.chat.id)

    # Step 1: auto-approve
    try:
        await bot.approve_chat_join_request(chat_id=request.chat.id, user_id=uid)
        log.info("Auto-approved join: user=%s chat=%s", uid, chat_title)
    except Exception as exc:
        log.warning("Approve failed user=%s: %s", uid, exc)
        return

    # Tiny delay to let Telegram propagate the new membership state
    await asyncio.sleep(1.0)

    # Step 2: user must exist (they did /start earlier via JOIN button)
    user = await asyncio.to_thread(get_user, uid)
    if not user:
        await safe_send(bot, uid,
            f"{ce(E_SUCCESS)}  <b>Joined {_html_escape.escape(chat_title)}!</b>\n\n"
            f"{ce(E_WARN)}  Send /start to the bot to unlock your dashboard.",
        )
        return

    # Step 3: check remaining channels
    missing, errors = await check_force_join(uid)

    if missing:
        remaining = "\n".join(f"   {ce(E_LINK)}  <b>{_html_escape.escape(m['title'])}</b>"
                              for m in missing)
        await safe_send(bot, uid,
            f"{ce(E_SUCCESS)}  <b>Joined {_html_escape.escape(chat_title)}</b>\n"
            f"{DIVIDER_THIN}\n\n"
            f"{ce(E_WARN)}  <i>You still need to join:</i>\n\n{remaining}\n\n"
            f"{ce(E_CROWN)}  Join all channels to unlock your dashboard.",
            reply_markup=force_join_kb(missing),
        )
        return

    if errors:
        log.warning("Force-join errors for %s: %s", uid, errors)

    # Step 4: all joined — auto-verify + credit referrer
    newly_verified, referrer_id, reward = await asyncio.to_thread(
        mark_verified_and_credit, uid)

    if newly_verified and referrer_id and reward:
        try:
            await safe_send(bot, referrer_id,
                f"{ce(E_SUCCESS)}  <b>NEW REFERRAL!</b>\n"
                f"{DIVIDER_THIN}\n\n"
                f"{ce(E_USERS)}  <b>{_html_escape.escape(fname or 'A user')}</b> joined via your link.\n"
                f"{ce(E_MONEY)}  You earned <b>{fmt_money(reward)}</b>.",
            )
        except Exception:
            pass

    # Step 5: push dashboard immediately
    await safe_send(bot, uid,
        f"{ce(E_SUCCESS)}  <b>VERIFIED!</b>  {ce(E_CROWN)}\n"
        f"{DIVIDER}\n\n"
        f"{ce(E_WELCOME)}  <i>Welcome aboard, <b>{_html_escape.escape(fname or 'friend')}</b>!</i>\n\n"
        f"<blockquote>{ce(E_MONEY)}  <b>₹2.00</b> per successful referral\n"
        f"{ce(E_USERS)}  Invite unlimited friends\n"
        f"{ce(E_WITHDRAW)}  Withdraw starting at <b>₹10.00</b></blockquote>\n\n"
        f"{ce(E_GIFT)}  <i>Your dashboard is ready below.</i>",
        reply_markup=main_menu_kb(uid),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 12. /START
# ─────────────────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    try:
        await _handle_start(message, state)
    except Exception as exc:
        log.exception("cmd_start CRASHED: %s", exc)
        try:
            await bot.send_message(message.chat.id,
                "⚠️ Something went wrong. Please /start again.", parse_mode=None)
        except: pass


async def _handle_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    u = message.from_user
    if not u: return

    parts = message.text.split(maxsplit=1) if message.text else []
    referred_by = None
    if len(parts) > 1 and parts[1].isdigit():
        c = int(parts[1])
        if c != u.id: referred_by = c

    log.info("START user=%s ref=%s", u.id, referred_by)

    await asyncio.to_thread(register_user, u.id, u.username,
                            u.first_name, u.last_name, referred_by)

    missing, errors = await check_force_join(u.id)
    log.info("Force-join: missing=%d errors=%d", len(missing), len(errors))

    if missing:
        await safe_send(bot, message.chat.id,
            f"{ce(E_WARN)}  <b>JOIN REQUIRED</b>\n"
            f"{DIVIDER_THIN}\n\n"
            f"{ce(E_LINK)}  <i>Please join the channel(s) below to unlock {BOT_NAME}.</i>\n\n"
            f"{ce(E_CROWN)}  After joining, tap <b>VERIFY JOIN</b>.",
            reply_markup=force_join_kb(missing),
        )
        return

    if errors:
        errs = "\n".join(f"• {_html_escape.escape(e)}" for e in errors)
        await safe_send(bot, message.chat.id,
            f"{ce(E_WARN)}  <b>Membership check warning</b>\n\n{errs}\n\n"
            f"{ce(E_WARN)}  Bot must be admin in the channel.")

    newly_verified, referrer_id, reward = await asyncio.to_thread(
        mark_verified_and_credit, u.id)
    if newly_verified and referrer_id and reward:
        try:
            await safe_send(bot, referrer_id,
                f"{ce(E_SUCCESS)}  <b>NEW REFERRAL!</b>\n"
                f"{DIVIDER_THIN}\n\n"
                f"{ce(E_USERS)}  <b>{_html_escape.escape(u.first_name or 'A user')}</b> joined via your link.\n"
                f"{ce(E_MONEY)}  You earned <b>{fmt_money(reward)}</b>.")
        except Exception: pass

    await show_main_menu(message.chat.id, u.id)


@router.callback_query(F.data == "verify_join")
async def cb_verify_join(cb: CallbackQuery, state: FSMContext) -> None:
    u = cb.from_user
    missing, errors = await check_force_join(u.id)

    if missing:
        await safe_edit(bot, cb.message.chat.id, cb.message.message_id,
            f"{ce(E_WARN)}  <b>JOIN REQUIRED</b>\n"
            f"{DIVIDER_THIN}\n\n"
            f"{ce(E_LINK)}  <i>You still need to join:</i>\n\n"
            + "\n".join(f"   {ce(E_LINK)}  <b>{_html_escape.escape(m['title'])}</b>" for m in missing)
            + f"\n\n{ce(E_CROWN)}  Tap <b>VERIFY JOIN</b> when done.",
            reply_markup=force_join_kb(missing))
        await safe_answer(cb, "Join all required channels first.", alert=True)
        return

    if errors:
        await safe_answer(cb, "Cannot verify. Contact support.", alert=True)
        return

    newly_verified, referrer_id, reward = await asyncio.to_thread(
        mark_verified_and_credit, u.id)
    if newly_verified and referrer_id and reward:
        try:
            await safe_send(bot, referrer_id,
                f"{ce(E_SUCCESS)}  <b>NEW REFERRAL!</b>\n"
                f"{DIVIDER_THIN}\n\n"
                f"{ce(E_USERS)}  <b>{_html_escape.escape(u.first_name or 'A user')}</b> joined.\n"
                f"{ce(E_MONEY)}  You earned <b>{fmt_money(reward)}</b>.")
        except Exception: pass

    await safe_answer(cb, "✅ Verified!", alert=False)
    await show_main_menu(cb.message.chat.id, u.id, edit_id=cb.message.message_id)


# ─────────────────────────────────────────────────────────────────────────────
# 13. ACCOUNT / REFER / BALANCE / STATS / HELP
# ─────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "account")
async def cb_account(cb: CallbackQuery) -> None:
    user = await asyncio.to_thread(get_user, cb.from_user.id)
    if not user:
        await safe_answer(cb, "Please /start first.", alert=True); return
    name = _html_escape.escape(user['first_name'] or '—')
    text = (
        f"{ce(E_STAR)}  <b>MY ACCOUNT</b>  {ce(E_CROWN)}\n"
        f"{DIVIDER}\n\n"
        f"<blockquote>{ce(E_USERS)}  <b>{name}</b>\n"
        f"{ce(E_SUPPORT)}  <code>{user['user_id']}</code>\n"
        f"{ce(E_CALENDAR)}  <i>Member since</i>  {user['created_at'][:10]}</blockquote>\n\n"
        f"{ce(E_LINK)}  <b>Referral Link</b>\n"
        f"<code>{referral_link(user['user_id'])}</code>\n\n"
        f"{DIVIDER_THIN}\n"
        f"{ce(E_STATS)}  <b>Statistics</b>\n\n"
        f"{ce(E_USERS)}  Total referrals      <b>{user['total_referrals']}</b>\n"
        f"{ce(E_SUCCESS)}  Successful           <b>{user['successful_referrals']}</b>\n"
        f"{ce(E_MONEY)}  Total earned         <b>{fmt_money(user['total_earned_paise'])}</b>\n\n"
        f"{ce(E_BALANCE)}  Available            <b>{fmt_money(user['balance_paise'])}</b>\n"
        f"{ce(E_WITHDRAW)}  Reserved             <b>{fmt_money(user['reserved_paise'])}</b>\n"
        f"{ce(E_HISTORY)}  Total paid           <b>{fmt_money(user['total_withdrawn_paise'])}</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("COPY REFERRAL LINK", copy_text=referral_link(user["user_id"]),
             style="primary", emoji_index=E_LINK)],
        [btn("REFER NOW", cb="refer", style="success", emoji_index=E_USERS)],
        [btn("BACK", cb="back_main", style="danger", emoji_index=E_REMOVE)],
    ])
    await safe_edit(bot, cb.message.chat.id, cb.message.message_id, text, kb)


@router.callback_query(F.data == "refer")
async def cb_refer(cb: CallbackQuery) -> None:
    user = await asyncio.to_thread(get_user, cb.from_user.id)
    if not user:
        await safe_answer(cb, "Please /start first.", alert=True); return
    link = referral_link(user["user_id"])
    share = f"https://t.me/share/url?url={link}&text=Join+{BOT_NAME.replace(' ', '+')}+and+earn+₹2+per+referral!"
    text = (
        f"{ce(E_USERS)}  <b>REFER &amp; EARN</b>  {ce(E_GIFT)}\n"
        f"{DIVIDER}\n\n"
        f"{ce(E_MONEY)}  Earn <b>₹2.00</b> for every friend who joins and verifies.\n\n"
        f"<blockquote>{ce(E_LINK)}  <b>Your Referral Link</b>\n"
        f"<code>{link}</code></blockquote>\n\n"
        f"{DIVIDER_THIN}\n"
        f"{ce(E_STATS)}  <b>Progress</b>\n\n"
        f"{ce(E_USERS)}  Successful referrals  <b>{user['successful_referrals']}</b>\n"
        f"{ce(E_BALANCE)}  Referral earnings      <b>{fmt_money(user['total_earned_paise'])}</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("COPY LINK", copy_text=link, style="primary", emoji_index=E_LINK)],
        [btn("SHARE LINK", url=share, style="success", emoji_index=E_GIFT)],
        [btn("BACK", cb="back_main", style="danger", emoji_index=E_REMOVE)],
    ])
    await safe_edit(bot, cb.message.chat.id, cb.message.message_id, text, kb)


@router.callback_query(F.data == "balance")
async def cb_balance(cb: CallbackQuery) -> None:
    user = await asyncio.to_thread(get_user, cb.from_user.id)
    if not user:
        await safe_answer(cb, "Please /start first.", alert=True); return
    text = (
        f"{ce(E_BALANCE)}  <b>MY BALANCE</b>  {ce(E_MONEY)}\n"
        f"{DIVIDER}\n\n"
        f"<blockquote>{ce(E_MONEY)}  Total earned      <b>{fmt_money(user['total_earned_paise'])}</b>\n"
        f"{ce(E_SUCCESS)}  Available         <b>{fmt_money(user['balance_paise'])}</b>\n"
        f"{ce(E_WITHDRAW)}  Reserved          <b>{fmt_money(user['reserved_paise'])}</b></blockquote>\n\n"
        f"{DIVIDER_THIN}\n"
        f"{ce(E_HISTORY)}  <b>Withdrawals</b>\n\n"
        f"{ce(E_WITHDRAW)}  Pending           <b>{fmt_money(user['reserved_paise'])}</b>\n"
        f"{ce(E_APPROVE)}  Total paid        <b>{fmt_money(user['total_withdrawn_paise'])}</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("WITHDRAW", cb="withdraw", style="success", emoji_index=E_WITHDRAW)],
        [btn("TRANSACTION HISTORY", cb="history", style="primary", emoji_index=E_HISTORY)],
        [btn("BACK", cb="back_main", style="danger", emoji_index=E_REMOVE)],
    ])
    await safe_edit(bot, cb.message.chat.id, cb.message.message_id, text, kb)


@router.callback_query(F.data == "history")
async def cb_history(cb: CallbackQuery) -> None:
    txs = await asyncio.to_thread(user_transactions, cb.from_user.id)
    lines = [f"{ce(E_HISTORY)}  <b>TRANSACTION HISTORY</b>", DIVIDER, ""]
    if not txs:
        lines.append(f"{ce(E_WARN)}  <i>No transactions yet.</i>")
    else:
        for t in txs:
            sign = "+" if t["amount_paise"] >= 0 else ""
            lines.append(
                f"{ce(E_CALENDAR)}  <i>{t['created_at'][:16].replace('T', ' ')}</i>\n"
                f"   <b>{sign}{fmt_money(t['amount_paise'])}</b>  ·  {t['type']}\n"
                f"   <i>{_html_escape.escape(t['description'] or '')}</i>\n")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("BACK", cb="balance", style="danger", emoji_index=E_REMOVE)]])
    await safe_edit(bot, cb.message.chat.id, cb.message.message_id, "\n".join(lines), kb)


@router.callback_query(F.data == "stats")
async def cb_stats(cb: CallbackQuery) -> None:
    user = await asyncio.to_thread(get_user, cb.from_user.id)
    if not user:
        await safe_answer(cb, "Please /start first.", alert=True); return
    s = await asyncio.to_thread(admin_stats)
    text = (
        f"{ce(E_STATS)}  <b>GLOBAL STATISTICS</b>  {ce(E_CROWN)}\n"
        f"{DIVIDER}\n\n"
        f"<blockquote>{ce(E_USERS)}  Total users            <b>{s['total_users']}</b>\n"
        f"{ce(E_LINK)}  Total referrals         <b>{s['total_referrals']}</b>\n"
        f"{ce(E_SUCCESS)}  Successful referrals  <b>{s['successful_referrals']}</b>\n"
        f"{ce(E_MONEY)}  Rewards paid          <b>{fmt_money(s['total_referral_rewards'])}</b></blockquote>\n\n"
        f"{ce(E_WITHDRAW)}  Pending withdrawals  <b>{s['pending_withdrawals']}</b>\n"
        f"{ce(E_APPROVE)}  Paid withdrawals     <b>{s['paid_withdrawals']}</b>\n\n"
        f"{DIVIDER_THIN}\n"
        f"{ce(E_BALANCE)}  <i>Your balance</i>  <b>{fmt_money(user['balance_paise'])}</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("BACK", cb="back_main", style="danger", emoji_index=E_REMOVE)]])
    await safe_edit(bot, cb.message.chat.id, cb.message.message_id, text, kb)


@router.callback_query(F.data == "how_to_earn")
async def cb_how_to_earn(cb: CallbackQuery) -> None:
    text = (
        f"{ce(E_HISTORY)}  <b>HOW TO EARN</b>  {ce(E_GIFT)}\n"
        f"{DIVIDER}\n\n"
        f"<blockquote>"
        f"<b>1.</b>  Share your referral link\n"
        f"<b>2.</b>  Friend opens the link and starts the bot\n"
        f"<b>3.</b>  Friend joins required channel(s)\n"
        f"<b>4.</b>  You earn <b>₹2.00</b> instantly"
        f"</blockquote>\n\n"
        f"{ce(E_WITHDRAW)}  Withdraw once you reach <b>₹10.00</b>\n\n"
        f"{DIVIDER_THIN}\n"
        f"{ce(E_WARN)}  <b>Rules</b>\n"
        f"   {ce(E_STAR)}  No self-referrals\n"
        f"   {ce(E_STAR)}  Each user can be referred once\n"
        f"   {ce(E_STAR)}  Rewards credited exactly once"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("BACK", cb="back_main", style="danger", emoji_index=E_REMOVE)]])
    await safe_edit(bot, cb.message.chat.id, cb.message.message_id, text, kb)


@router.callback_query(F.data == "support")
async def cb_support(cb: CallbackQuery) -> None:
    text = (
        f"{ce(E_SUPPORT)}  <b>SUPPORT</b>\n"
        f"{DIVIDER}\n\n"
        f"{ce(E_ADMIN)}  <i>Contact administrators:</i>\n\n"
        + "\n".join(f"   {ce(E_STAR)}  {u}" for u in ADMIN_USERNAMES)
        + f"\n\n{ce(E_WARN)}  <i>Never share your bot token or UPI PIN.</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("BACK", cb="back_main", style="danger", emoji_index=E_REMOVE)]])
    await safe_edit(bot, cb.message.chat.id, cb.message.message_id, text, kb)


@router.callback_query(F.data == "back_main")
async def cb_back_main(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await show_main_menu(cb.message.chat.id, cb.from_user.id, edit_id=cb.message.message_id)


# ─────────────────────────────────────────────────────────────────────────────
# 14. WITHDRAWAL FLOW
# ─────────────────────────────────────────────────────────────────────────────

class WithdrawStates(StatesGroup):
    waiting_amount = State()
    waiting_upi = State()
    waiting_confirm = State()


UPI_RE = re.compile(r"^[a-zA-Z0-9.\-_]{2,256}@[a-zA-Z]{2,64}$")


@router.callback_query(F.data == "withdraw")
async def cb_withdraw_start(cb: CallbackQuery, state: FSMContext) -> None:
    user = await asyncio.to_thread(get_user, cb.from_user.id)
    if not user:
        await safe_answer(cb, "Please /start first.", alert=True); return
    if user["balance_paise"] < MIN_WITHDRAWAL_PAISE:
        await safe_answer(cb,
            f"Minimum withdrawal is {fmt_money(MIN_WITHDRAWAL_PAISE)}. "
            f"Your balance is {fmt_money(user['balance_paise'])}.", alert=True)
        return
    await state.set_state(WithdrawStates.waiting_amount)
    text = (
        f"{ce(E_WITHDRAW)}  <b>WITHDRAW</b>  {ce(E_MONEY)}\n"
        f"{DIVIDER}\n\n"
        f"<blockquote>{ce(E_BALANCE)}  Available   <b>{fmt_money(user['balance_paise'])}</b>\n"
        f"{ce(E_WARN)}  Minimum     <b>{fmt_money(MIN_WITHDRAWAL_PAISE)}</b></blockquote>\n\n"
        f"{ce(E_STAR)}  <i>Enter the amount you want to withdraw</i>\n"
        f"   Example: <code>10</code> or <code>10.50</code>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("CANCEL", cb="back_main", style="danger", emoji_index=E_REMOVE)]])
    await safe_edit(bot, cb.message.chat.id, cb.message.message_id, text, kb)


@router.message(WithdrawStates.waiting_amount)
async def withdraw_amount_received(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip().replace("₹", "").replace(",", "")
    try: amount = float(raw)
    except ValueError:
        await safe_send(bot, message.chat.id, f"{ce(E_ERROR)}  <b>Invalid amount.</b> Send a number.")
        return
    ap = int(round(amount * 100))
    user = await asyncio.to_thread(get_user, message.from_user.id)
    if ap < MIN_WITHDRAWAL_PAISE:
        await safe_send(bot, message.chat.id,
            f"{ce(E_ERROR)}  Minimum withdrawal is <b>{fmt_money(MIN_WITHDRAWAL_PAISE)}</b>.")
        return
    if not user or ap > user["balance_paise"]:
        await safe_send(bot, message.chat.id,
            f"{ce(E_ERROR)}  Amount exceeds balance <b>{fmt_money(user['balance_paise']) if user else '₹0.00'}</b>.")
        return
    await state.update_data(amount_paise=ap)
    await state.set_state(WithdrawStates.waiting_upi)
    await safe_send(bot, message.chat.id,
        f"{ce(E_SUCCESS)}  Amount  <b>{fmt_money(ap)}</b>\n"
        f"{DIVIDER_THIN}\n\n"
        f"{ce(E_WITHDRAW)}  <i>Now send your UPI ID</i>\n"
        f"   Example: <code>name@okhdfcbank</code>")


@router.message(WithdrawStates.waiting_upi)
async def withdraw_upi_received(message: Message, state: FSMContext) -> None:
    upi = (message.text or "").strip()
    if not UPI_RE.match(upi):
        await safe_send(bot, message.chat.id,
            f"{ce(E_ERROR)}  Invalid UPI. Example: <code>name@okhdfcbank</code>")
        return
    d = await state.get_data()
    ap = int(d.get("amount_paise", 0))
    await state.update_data(upi_id=upi)
    await state.set_state(WithdrawStates.waiting_confirm)
    text = (
        f"{ce(E_APPROVE)}  <b>CONFIRM WITHDRAWAL</b>\n"
        f"{DIVIDER}\n\n"
        f"<blockquote>{ce(E_MONEY)}  Amount   <b>{fmt_money(ap)}</b>\n"
        f"{ce(E_WITHDRAW)}  UPI      <code>{_html_escape.escape(upi)}</code></blockquote>\n\n"
        f"{ce(E_WARN)}  <i>Amount will be reserved until an admin processes it.</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("CONFIRM", cb="withdraw_confirm", style="success", emoji_index=E_APPROVE)],
        [btn("CANCEL", cb="back_main", style="danger", emoji_index=E_REMOVE)]])
    await safe_send(bot, message.chat.id, text, reply_markup=kb)


@router.callback_query(F.data == "withdraw_confirm", WithdrawStates.waiting_confirm)
async def cb_withdraw_confirm(cb: CallbackQuery, state: FSMContext) -> None:
    d = await state.get_data()
    ap = int(d.get("amount_paise", 0))
    upi = d.get("upi_id", "")
    uid = cb.from_user.id
    if ap < MIN_WITHDRAWAL_PAISE or not UPI_RE.match(upi):
        await safe_answer(cb, "Invalid withdrawal. Restart.", alert=True)
        await state.clear(); return
    wid = f"WD{int(time.time())}{uid % 10000}"
    ok, _ = await asyncio.to_thread(create_withdrawal, wid, uid, ap, upi)
    if not ok:
        await safe_answer(cb, "Insufficient balance.", alert=True)
        await state.clear(); return
    await state.clear()
    user = await asyncio.to_thread(get_user, uid)
    await safe_edit(bot, cb.message.chat.id, cb.message.message_id,
        f"{ce(E_SUCCESS)}  <b>REQUEST CREATED</b>\n"
        f"{DIVIDER}\n\n"
        f"<blockquote>{ce(E_STAR)}  ID       <code>{wid}</code>\n"
        f"{ce(E_MONEY)}  Amount   <b>{fmt_money(ap)}</b>\n"
        f"{ce(E_WITHDRAW)}  UPI      <code>{_html_escape.escape(upi)}</code>\n"
        f"{ce(E_HISTORY)}  Status   <b>PENDING</b></blockquote>\n\n"
        f"{ce(E_WARN)}  <i>You will be notified once processed.</i>")
    admin_text = (
        f"{ce(E_WITHDRAW)}  <b>NEW WITHDRAWAL REQUEST</b>\n"
        f"{DIVIDER}\n\n"
        f"<blockquote>{ce(E_USERS)}  User ID   <code>{uid}</code>\n"
        f"{ce(E_SUPPORT)}  Username  @{user['username'] if user and user['username'] else '—'}\n"
        f"{ce(E_MONEY)}  Amount    <b>{fmt_money(ap)}</b>\n"
        f"{ce(E_WITHDRAW)}  UPI       <code>{_html_escape.escape(upi)}</code>\n"
        f"{ce(E_STAR)}  Request   <code>{wid}</code></blockquote>\n"
        f"{ce(E_CALENDAR)}  <i>{_now()[:16].replace('T', ' ')}</i>"
    )
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[[
        btn("APPROVE", cb=f"wd_approve:{wid}", style="success", emoji_index=E_APPROVE),
        btn("REJECT", cb=f"wd_reject:{wid}", style="danger", emoji_index=E_REJECT),
    ]])
    for aid in ADMIN_IDS:
        try: await safe_send(bot, aid, admin_text, reply_markup=admin_kb)
        except Exception as exc: log.warning("Admin notify %s: %s", aid, exc)
    await safe_answer(cb, "Request submitted!", alert=True)


# ─────────────────────────────────────────────────────────────────────────────
# 15. ADMIN WITHDRAWAL CALLBACKS
# ─────────────────────────────────────────────────────────────────────────────

def is_admin(uid: int) -> bool: return uid in ADMIN_IDS


@router.callback_query(F.data.startswith("wd_approve:"))
async def cb_wd_approve(cb: CallbackQuery) -> None:
    if not is_admin(cb.from_user.id):
        await safe_answer(cb, "Unauthorized.", alert=True); return
    wid = cb.data.split(":", 1)[1]
    ok, st, wd = await asyncio.to_thread(approve_withdrawal, wid, cb.from_user.id)
    if not ok:
        await safe_answer(cb, f"Cannot approve: {st}.", alert=True); return
    await safe_edit(bot, cb.message.chat.id, cb.message.message_id,
        f"{ce(E_APPROVE)}  <b>APPROVED</b>\n"
        f"{DIVIDER}\n\n"
        f"{ce(E_STAR)}  Request <code>{wid}</code> approved.\n"
        f"{ce(E_WARN)}  <i>Transfer payment, then tap MARK AS PAID.</i>",
        InlineKeyboardMarkup(inline_keyboard=[[
            btn("MARK AS PAID", cb=f"wd_paid:{wid}", style="success", emoji_index=E_MONEY)
        ]]))
    await safe_send(bot, wd["user_id"],
        f"{ce(E_APPROVE)}  <b>Withdrawal Approved</b>\n"
        f"{DIVIDER_THIN}\n\n"
        f"<blockquote>{ce(E_STAR)}  ID       <code>{wid}</code>\n"
        f"{ce(E_MONEY)}  Amount   <b>{fmt_money(wd['amount_paise'])}</b>\n"
        f"{ce(E_WITHDRAW)}  UPI      <code>{_html_escape.escape(wd['upi_id'])}</code></blockquote>\n\n"
        f"{ce(E_WARN)}  <i>Awaiting payment. You will be notified when sent.</i>")
    await safe_answer(cb, "Approved. Mark paid after transfer.")


@router.callback_query(F.data.startswith("wd_paid:"))
async def cb_wd_paid(cb: CallbackQuery) -> None:
    if not is_admin(cb.from_user.id):
        await safe_answer(cb, "Unauthorized.", alert=True); return
    wid = cb.data.split(":", 1)[1]
    ok, st, wd = await asyncio.to_thread(mark_withdrawal_paid, wid, cb.from_user.id)
    if not ok:
        await safe_answer(cb, f"Cannot mark paid: {st}.", alert=True); return
    await safe_edit(bot, cb.message.chat.id, cb.message.message_id,
        f"{ce(E_SUCCESS)}  <b>PAID</b>\n\nRequest <code>{wid}</code> marked paid.")
    await safe_send(bot, wd["user_id"],
        f"{ce(E_SUCCESS)}  <b>Payment Completed</b>\n"
        f"{DIVIDER}\n\n"
        f"<i>Your withdrawal has been approved.</i>\n\n"
        f"<blockquote>The payment of <b>{fmt_money(wd['amount_paise'])}</b> has been sent to your registered UPI ID:\n"
        f"<code>{_html_escape.escape(wd['upi_id'])}</code></blockquote>\n\n"
        f"{ce(E_CROWN)}  <i>Thank you for using {BOT_NAME}.</i>")
    await safe_answer(cb, "Marked paid. User notified.")


@router.callback_query(F.data.startswith("wd_reject:"))
async def cb_wd_reject(cb: CallbackQuery) -> None:
    if not is_admin(cb.from_user.id):
        await safe_answer(cb, "Unauthorized.", alert=True); return
    wid = cb.data.split(":", 1)[1]
    ok, st, wd = await asyncio.to_thread(reject_withdrawal, wid, cb.from_user.id, "Rejected")
    if not ok:
        await safe_answer(cb, f"Cannot reject: {st}.", alert=True); return
    await safe_edit(bot, cb.message.chat.id, cb.message.message_id,
        f"{ce(E_REJECT)}  <b>REJECTED</b>\n\nRequest <code>{wid}</code> rejected and refunded.")
    await safe_send(bot, wd["user_id"],
        f"{ce(E_REJECT)}  <b>Withdrawal Rejected</b>\n"
        f"{DIVIDER_THIN}\n\n"
        f"<blockquote>{ce(E_STAR)}  ID       <code>{wid}</code>\n"
        f"{ce(E_MONEY)}  Amount   <b>{fmt_money(wd['amount_paise'])}</b></blockquote>\n\n"
        f"{ce(E_SUCCESS)}  <i>Amount has been returned to your balance.</i>")
    await safe_answer(cb, "Rejected and refunded.")


# ─────────────────────────────────────────────────────────────────────────────
# 16. ADMIN PANEL
# ─────────────────────────────────────────────────────────────────────────────

class PromoStates(StatesGroup): waiting_link = State()
class BroadcastStates(StatesGroup): waiting_message = State()


def admin_panel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("ADMIN DASHBOARD", cb="admin_dashboard", style="primary", emoji_index=E_ADMIN)],
        [btn("ALL USERS", cb="admin_all_users", style="success", emoji_index=E_USERS)],
        [btn("USER LIST", cb="admin_users:0", style="primary", emoji_index=E_SEARCH)],
        [btn("WITHDRAWAL REQUESTS", cb="admin_withdrawals", style="success", emoji_index=E_WITHDRAW)],
        [btn("ADD PROMO CHANNEL", cb="admin_add_promo", style="primary", emoji_index=E_ADD)],
        [btn("REMOVE PROMO CHANNEL", cb="admin_remove_promo", style="danger", emoji_index=E_REMOVE)],
        [btn("BROADCAST", cb="admin_broadcast", style="success", emoji_index=E_BROADCAST)],
        [btn("BOT STATISTICS", cb="admin_stats", style="primary", emoji_index=E_STATS)],
        [btn("CLOSE PANEL", cb="back_main", style="danger", emoji_index=E_CLOSE)],
    ])


@router.callback_query(F.data == "admin_panel")
async def cb_admin_panel(cb: CallbackQuery) -> None:
    if not is_admin(cb.from_user.id):
        await safe_answer(cb, "Unauthorized.", alert=True); return
    await safe_edit(bot, cb.message.chat.id, cb.message.message_id,
        f"{ce(E_ADMIN)}  <b>ADMIN PANEL</b>\n"
        f"{DIVIDER}\n\n"
        f"{ce(E_CROWN)}  <i>Authorized administrators:</i>\n"
        + "\n".join(f"   {ce(E_STAR)}  {u}" for u in ADMIN_USERNAMES),
        admin_panel_kb())
    await safe_answer(cb, "Admin panel opened.")


@router.callback_query(F.data == "admin_dashboard")
async def cb_admin_dashboard(cb: CallbackQuery) -> None:
    if not is_admin(cb.from_user.id):
        await safe_answer(cb, "Unauthorized.", alert=True); return
    s = await asyncio.to_thread(admin_stats)
    text = (
        f"{ce(E_ADMIN)}  <b>ADMIN DASHBOARD</b>\n"
        f"{DIVIDER}\n\n"
        f"<blockquote>{ce(E_USERS)}  Total users             <b>{s['total_users']}</b>\n"
        f"{ce(E_LINK)}  Total referrals         <b>{s['total_referrals']}</b>\n"
        f"{ce(E_SUCCESS)}  Successful referrals  <b>{s['successful_referrals']}</b>\n"
        f"{ce(E_MONEY)}  Referral rewards      <b>{fmt_money(s['total_referral_rewards'])}</b></blockquote>\n\n"
        f"{ce(E_WITHDRAW)}  Pending withdrawals   <b>{s['pending_withdrawals']}</b>\n"
        f"{ce(E_APPROVE)}  Approved             <b>{s['approved_withdrawals']}</b>\n"
        f"{ce(E_HISTORY)}  Paid                 <b>{s['paid_withdrawals']}</b>\n\n"
        f"{DIVIDER_THIN}\n"
        f"{ce(E_BALANCE)}  Available balance     <b>{fmt_money(s['total_available_balance'])}</b>\n"
        f"{ce(E_WARN)}  Reserved balance      <b>{fmt_money(s['total_reserved_balance'])}</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("BACK", cb="admin_panel", style="danger", emoji_index=E_REMOVE)]])
    await safe_edit(bot, cb.message.chat.id, cb.message.message_id, text, kb)


@router.callback_query(F.data == "admin_all_users")
async def cb_admin_all_users(cb: CallbackQuery) -> None:
    if not is_admin(cb.from_user.id):
        await safe_answer(cb, "Unauthorized.", alert=True); return
    total = await asyncio.to_thread(count_users)
    await safe_answer(cb, f"Total users: {total}", alert=True)


@router.callback_query(F.data.startswith("admin_users:"))
async def cb_admin_users_page(cb: CallbackQuery) -> None:
    if not is_admin(cb.from_user.id):
        await safe_answer(cb, "Unauthorized.", alert=True); return
    page = int(cb.data.split(":", 1)[1])
    users = await asyncio.to_thread(list_users_page, page)
    total = await asyncio.to_thread(count_users)
    lines = [f"{ce(E_USERS)}  <b>USER LIST</b>  ·  page {page + 1}", DIVIDER, ""]
    if not users: lines.append("<i>No users on this page.</i>")
    for u in users:
        lines.append(
            f"{ce(E_SUPPORT)}  <code>{u['user_id']}</code>  @{u['username'] or '—'}\n"
            f"   {ce(E_BALANCE)}  {fmt_money(u['balance_paise'])}  ·  "
            f"{ce(E_USERS)}  {u['successful_referrals']} refs  ·  "
            f"{ce(E_CALENDAR)}  {u['created_at'][:10]}\n")
    nav = []
    if page > 0: nav.append(btn("‹ PREV", cb=f"admin_users:{page - 1}",
                                style="primary", emoji_index=E_REMOVE))
    if (page + 1) * 10 < total: nav.append(btn("NEXT ›", cb=f"admin_users:{page + 1}",
                                               style="success", emoji_index=E_ADD))
    rows = [nav] if nav else []
    rows.append([btn("BACK", cb="admin_panel", style="danger", emoji_index=E_REMOVE)])
    await safe_edit(bot, cb.message.chat.id, cb.message.message_id,
                    "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "admin_withdrawals")
async def cb_admin_withdrawals(cb: CallbackQuery) -> None:
    if not is_admin(cb.from_user.id):
        await safe_answer(cb, "Unauthorized.", alert=True); return
    pending = await asyncio.to_thread(list_pending_withdrawals)
    if not pending:
        await safe_answer(cb, "No pending withdrawals.", alert=True); return
    for wd in pending:
        text = (
            f"{ce(E_WITHDRAW)}  <b>PENDING WITHDRAWAL</b>\n"
            f"{DIVIDER_THIN}\n\n"
            f"<blockquote>{ce(E_STAR)}  ID       <code>{wd['id']}</code>\n"
            f"{ce(E_USERS)}  User     <code>{wd['user_id']}</code>\n"
            f"{ce(E_MONEY)}  Amount   <b>{fmt_money(wd['amount_paise'])}</b>\n"
            f"{ce(E_WITHDRAW)}  UPI      <code>{_html_escape.escape(wd['upi_id'])}</code></blockquote>\n"
            f"{ce(E_CALENDAR)}  <i>{wd['requested_at'][:16].replace('T', ' ')}</i>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            btn("APPROVE", cb=f"wd_approve:{wd['id']}", style="success", emoji_index=E_APPROVE),
            btn("REJECT", cb=f"wd_reject:{wd['id']}", style="danger", emoji_index=E_REJECT),
        ]])
        await safe_send(bot, cb.message.chat.id, text, reply_markup=kb)
    await safe_answer(cb, f"{len(pending)} pending.")


@router.callback_query(F.data == "admin_add_promo")
async def cb_admin_add_promo(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(cb.from_user.id):
        await safe_answer(cb, "Unauthorized.", alert=True); return
    await state.set_state(PromoStates.waiting_link)
    await safe_edit(bot, cb.message.chat.id, cb.message.message_id,
        f"{ce(E_ADD)}  <b>ADD PROMO CHANNEL</b>\n"
        f"{DIVIDER}\n\n"
        f"{ce(E_LINK)}  <b>Public channel</b>\n"
        f"   <code>https://t.me/yourchannel</code>\n\n"
        f"{ce(E_WARN)}  <b>Private channel</b>\n"
        f"   <code>-1001234567890</code>\n\n"
        f"{DIVIDER_THIN}\n"
        f"{ce(E_CROWN)}  <b>Bot will auto:</b>\n"
        f"   {ce(E_STAR)}  Create invite link\n"
        f"   {ce(E_STAR)}  Auto-approve join requests\n"
        f"   {ce(E_STAR)}  Auto-verify users and open dashboard\n\n"
        f"{ce(E_ERROR)}  <b>Requirements:</b>\n"
        f"   Bot must be Administrator in the channel\n"
        f"   With <b>Invite Users via link</b> permission",
        InlineKeyboardMarkup(inline_keyboard=[[
            btn("CANCEL", cb="admin_panel", style="danger", emoji_index=E_REMOVE)]]))


@router.message(PromoStates.waiting_link)
async def promo_link_received(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id): return
    raw = (message.text or "").strip()
    if not raw:
        await safe_send(bot, message.chat.id, f"{ce(E_ERROR)}  Send a link or numeric ID.")
        return
    try:
        if raw.lstrip("-").isdigit(): chat_id = int(raw)
        elif raw.startswith("https://t.me/") or raw.startswith("@"):
            chat_id = "@" + raw.rstrip("/").split("/")[-1].lstrip("@")
        else: chat_id = "@" + raw.lstrip("@")
        chat = await bot.get_chat(chat_id)
        member = await bot.get_chat_member(chat.id, bot.id)
        if member.status not in ("administrator", "creator"):
            await safe_send(bot, message.chat.id,
                f"{ce(E_ERROR)}  Bot is NOT an admin in <b>{_html_escape.escape(chat.title or str(chat.id))}</b>.\n\n"
                f"{ce(E_WARN)}  Add bot as admin with <b>Invite Users via link</b>.")
            await state.clear(); return
        invite_link = None
        if chat.username:
            invite_link = f"https://t.me/{chat.username}"
        else:
            try:
                obj = await bot.create_chat_invite_link(
                    chat_id=chat.id, creates_join_request=True,
                    name=f"CytunBot_{int(time.time())}")
                invite_link = obj.invite_link
            except Exception as exc:
                await safe_send(bot, message.chat.id,
                    f"{ce(E_ERROR)}  Cannot create invite link.\n"
                    f"{ce(E_WARN)}  Give bot <b>Invite Users via link</b> permission.\n"
                    f"<code>{_html_escape.escape(str(exc))}</code>")
                await state.clear(); return
        ident = f"@{chat.username}" if chat.username else str(chat.id)
        await asyncio.to_thread(add_or_update_promo_channel,
                                raw, ident, chat.title or str(chat.id),
                                message.from_user.id, invite_link)
        await state.clear()
        await safe_send(bot, message.chat.id,
            f"{ce(E_SUCCESS)}  <b>PROMO CHANNEL ADDED</b>\n"
            f"{DIVIDER}\n\n"
            f"{ce(E_CROWN)}  <b>{_html_escape.escape(chat.title or str(chat.id))}</b>\n"
            f"{ce(E_SUPPORT)}  <code>{ident}</code>\n"
            f"{ce(E_LINK)}  {invite_link}\n\n"
            f"{ce(E_APPROVE)}  <i>Auto-approve ON · Auto-verify ON</i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                btn("BACK", cb="admin_panel", style="danger", emoji_index=E_REMOVE)]]))
    except Exception as exc:
        log.warning("Add promo failed: %s", exc)
        await safe_send(bot, message.chat.id,
            f"{ce(E_ERROR)}  Could not resolve channel.\n\n"
            f"{ce(E_WARN)}  For private, send numeric ID and add bot as admin.\n\n"
            f"<code>{_html_escape.escape(str(exc))}</code>")
        await state.clear()


@router.callback_query(F.data == "admin_remove_promo")
async def cb_admin_remove_promo(cb: CallbackQuery) -> None:
    if not is_admin(cb.from_user.id):
        await safe_answer(cb, "Unauthorized.", alert=True); return
    chans = await asyncio.to_thread(list_promo_channels)
    if not chans:
        await safe_answer(cb, "No promo channels configured.", alert=True); return
    rows = [[btn(f"REMOVE  ›  {(c['title'] or c['channel_link'])[:32]}",
                 cb=f"promo_remove_confirm:{c['id']}",
                 style="danger", emoji_index=E_REMOVE)] for c in chans]
    rows.append([btn("BACK", cb="admin_panel", style="primary", emoji_index=E_REMOVE)])
    await safe_edit(bot, cb.message.chat.id, cb.message.message_id,
        f"{ce(E_REMOVE)}  <b>REMOVE PROMO CHANNEL</b>\n"
        f"{DIVIDER}\n\n<i>Select a channel to remove:</i>",
        InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("promo_remove_confirm:"))
async def cb_promo_remove_confirm(cb: CallbackQuery) -> None:
    if not is_admin(cb.from_user.id):
        await safe_answer(cb, "Unauthorized.", alert=True); return
    cid = int(cb.data.split(":", 1)[1])
    await asyncio.to_thread(remove_promo_channel, cid)
    await safe_answer(cb, "Removed.", alert=True)
    await safe_edit(bot, cb.message.chat.id, cb.message.message_id,
        f"{ce(E_SUCCESS)}  Promo channel removed.",
        InlineKeyboardMarkup(inline_keyboard=[[
            btn("BACK", cb="admin_panel", style="danger", emoji_index=E_REMOVE)]]))


@router.callback_query(F.data == "admin_broadcast")
async def cb_admin_broadcast(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(cb.from_user.id):
        await safe_answer(cb, "Unauthorized.", alert=True); return
    await state.set_state(BroadcastStates.waiting_message)
    await safe_edit(bot, cb.message.chat.id, cb.message.message_id,
        f"{ce(E_BROADCAST)}  <b>BROADCAST</b>\n"
        f"{DIVIDER}\n\n"
        f"{ce(E_STAR)}  <i>Send the message to broadcast.</i>\n"
        f"{ce(E_WARN)}  Will be copied to every user.",
        InlineKeyboardMarkup(inline_keyboard=[[
            btn("CANCEL", cb="admin_panel", style="danger", emoji_index=E_REMOVE)]]))


@router.message(BroadcastStates.waiting_message)
async def broadcast_message_received(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id): return
    await state.clear()
    await safe_send(bot, message.chat.id, f"{ce(E_BROADCAST)}  <i>Broadcasting…</i>")
    with db() as c:
        users = c.execute("SELECT user_id FROM users").fetchall()
    ok, fail = 0, 0
    for r in users:
        try:
            await bot.copy_message(chat_id=r["user_id"],
                                   from_chat_id=message.chat.id,
                                   message_id=message.message_id)
            ok += 1
        except Exception: fail += 1
        await asyncio.sleep(0.05)
    await safe_send(bot, message.chat.id,
        f"{ce(E_SUCCESS)}  <b>BROADCAST DONE</b>\n"
        f"{DIVIDER_THIN}\n\n"
        f"{ce(E_USERS)}  Recipients  <b>{len(users)}</b>\n"
        f"{ce(E_APPROVE)}  Delivered   <b>{ok}</b>\n"
        f"{ce(E_REJECT)}  Failed      <b>{fail}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            btn("BACK", cb="admin_panel", style="danger", emoji_index=E_REMOVE)]]))


@router.callback_query(F.data == "admin_stats")
async def cb_admin_stats(cb: CallbackQuery) -> None:
    if not is_admin(cb.from_user.id):
        await safe_answer(cb, "Unauthorized.", alert=True); return
    s = await asyncio.to_thread(admin_stats)
    text = (
        f"{ce(E_STATS)}  <b>BOT STATISTICS</b>\n"
        f"{DIVIDER}\n\n"
        f"<blockquote>{ce(E_USERS)}  Total users            <b>{s['total_users']}</b>\n"
        f"{ce(E_LINK)}  Total referrals        <b>{s['total_referrals']}</b>\n"
        f"{ce(E_SUCCESS)}  Successful             <b>{s['successful_referrals']}</b>\n"
        f"{ce(E_MONEY)}  Rewards paid          <b>{fmt_money(s['total_referral_rewards'])}</b></blockquote>\n\n"
        f"{ce(E_WITHDRAW)}  Pending   <b>{s['pending_withdrawals']}</b>\n"
        f"{ce(E_APPROVE)}  Approved  <b>{s['approved_withdrawals']}</b>\n"
        f"{ce(E_HISTORY)}  Paid      <b>{s['paid_withdrawals']}</b>\n\n"
        f"{ce(E_BALANCE)}  Available  <b>{fmt_money(s['total_available_balance'])}</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("BACK", cb="admin_panel", style="danger", emoji_index=E_REMOVE)]])
    await safe_edit(bot, cb.message.chat.id, cb.message.message_id, text, kb)


# ─────────────────────────────────────────────────────────────────────────────
# 17. FLASK WEBSITE + WEBHOOK
# ─────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)
_loop: Optional[asyncio.AbstractEventLoop] = None
_loop_ready = threading.Event()
_loop_started = False
_loop_lock = threading.Lock()


async def _startup_check() -> None:
    try:
        me = await bot.me()
        m = await bot.get_chat_member(MAIN_CHANNEL_USERNAME, me.id)
        if m.status in ("administrator", "creator"):
            log.info("✅ Bot is admin in %s", MAIN_CHANNEL_USERNAME)
        else:
            log.warning("⚠️ Bot NOT admin in %s — force-join will fail!", MAIN_CHANNEL_USERNAME)
    except Exception as exc:
        log.warning("⚠️ Cannot verify admin in %s: %s", MAIN_CHANNEL_USERNAME, exc)


async def _set_webhook() -> None:
    try:
        me = await bot.me()
        global _bot_username_cache
        _bot_username_cache = me.username
        log.info("Bot authenticated as @%s", me.username)
    except Exception as exc:
        log.exception("Auth FAILED: %s", exc); return
    if not WEBHOOK_URL or not WEBHOOK_SECRET:
        log.error("WEBHOOK_URL or WEBHOOK_SECRET missing"); return
    url = f"{WEBHOOK_URL}/webhook/{WEBHOOK_SECRET}"
    try:
        await bot.set_webhook(url=url, secret_token=WEBHOOK_SECRET,
                              drop_pending_updates=True,
                              allowed_updates=["message", "callback_query", "chat_join_request"])
        log.info("Webhook set: %s", url)
    except Exception as exc:
        log.exception("Webhook failed: %s", exc)


def _run_bg_loop() -> None:
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    try:
        _loop.run_until_complete(_set_webhook())
        _loop.run_until_complete(_startup_check())
    except Exception as exc:
        log.exception("BG init: %s", exc)
    finally:
        _loop_ready.set()
    _loop.run_forever()


def ensure_loop() -> None:
    global _loop_started
    with _loop_lock:
        if _loop_started: return
        threading.Thread(target=_run_bg_loop, daemon=True, name="aiogram-loop").start()
        if not _loop_ready.wait(15):
            log.error("BG loop not ready")
        _loop_started = True


@app.before_request
def _ensure(): ensure_loop()


@app.route("/webhook/<secret>", methods=["POST"])
def telegram_webhook(secret: str):
    if secret != WEBHOOK_SECRET:
        return jsonify({"ok": False}), 403
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token", "") != WEBHOOK_SECRET:
        return jsonify({"ok": False}), 403
    ensure_loop()
    data = request.get_json(force=True, silent=True)
    if not data: return jsonify({"ok": False}), 400
    try: update = Update.model_validate(data)
    except Exception: return jsonify({"ok": False}), 400
    if not _loop or not _loop.is_running():
        return jsonify({"ok": False}), 503
    fut = asyncio.run_coroutine_threadsafe(dp.feed_webhook_update(bot, update), _loop)
    def _cb(f):
        try:
            e = f.exception()
            if e: log.error("Update failed: %s", e)
        except: pass
    fut.add_done_callback(_cb)
    return jsonify({"ok": True}), 200


@app.route("/health")
def health():
    return jsonify({"status": "ok", "bot": BOT_NAME, "visits": get_visit_count()})


WEBSITE_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cytun Refer Bot — Earn ₹2 per referral</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700;800;900&family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{--bg:#030014;--card:rgba(20,15,45,.55);--line:rgba(255,255,255,.08);
--text:#f5f3ff;--muted:#a5a3c2;--green:#22c55e;--purple:#a78bfa;--cyan:#22d3ee}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{min-height:100vh;display:flex;align-items:center;justify-content:center;
background:var(--bg);color:var(--text);font-family:'Inter',system-ui,sans-serif;
padding:24px;overflow-x:hidden;position:relative;-webkit-font-smoothing:antialiased}
.orb{position:fixed;border-radius:50%;filter:blur(100px);opacity:.45;
pointer-events:none;z-index:0;animation:float 14s ease-in-out infinite alternate}
.orb-1{width:500px;height:500px;background:#7c3aed;top:-200px;left:-150px}
.orb-2{width:420px;height:420px;background:#06b6d4;bottom:-160px;right:-100px;animation-delay:-5s}
.orb-3{width:300px;height:300px;background:#22c55e;top:40%;right:28%;animation-delay:-9s;opacity:.22}
@keyframes float{0%{transform:translate(0,0) scale(1)}100%{transform:translate(30px,-30px) scale(1.12)}}
.grid-bg{position:fixed;inset:0;background-image:
linear-gradient(rgba(139,92,246,.06) 1px,transparent 1px),
linear-gradient(90deg,rgba(139,92,246,.06) 1px,transparent 1px);
background-size:48px 48px;z-index:0;pointer-events:none;
mask-image:radial-gradient(circle at center,black 30%,transparent 80%);
-webkit-mask-image:radial-gradient(circle at center,black 30%,transparent 80%)}
.card{position:relative;z-index:1;width:100%;max-width:760px;padding:52px 44px 44px;
border-radius:32px;background:var(--card);border:1px solid var(--line);
backdrop-filter:blur(24px) saturate(140%);-webkit-backdrop-filter:blur(24px) saturate(140%);
box-shadow:0 40px 120px rgba(0,0,0,.6),0 0 0 1px rgba(255,255,255,.03) inset,0 1px 0 rgba(255,255,255,.06) inset;
text-align:center;overflow:hidden}
.card::before{content:'';position:absolute;top:-50%;left:-50%;width:200%;height:200%;
background:conic-gradient(from 0deg,transparent,rgba(139,92,246,.15),transparent,rgba(34,211,238,.15),transparent);
animation:rotate 20s linear infinite;z-index:-1;opacity:.35}
@keyframes rotate{100%{transform:rotate(360deg)}}
.badge{display:inline-flex;gap:8px;align-items:center;padding:9px 16px;
border-radius:999px;border:1px solid rgba(167,139,250,.3);
background:rgba(167,139,250,.08);color:var(--purple);font-size:11px;
font-weight:600;letter-spacing:.15em;text-transform:uppercase;font-family:'Outfit',sans-serif}
.badge::before{content:'';width:6px;height:6px;border-radius:50%;background:var(--green);
box-shadow:0 0 12px var(--green);animation:blink 2s ease-in-out infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
h1{margin:26px 0 14px;font-family:'Outfit',sans-serif;font-weight:900;
font-size:clamp(34px,6vw,58px);line-height:1.02;letter-spacing:-.035em;
background:linear-gradient(135deg,#fff 0%,#c4b5fd 40%,#22d3ee 70%,#4ade80 100%);
-webkit-background-clip:text;background-clip:text;color:transparent;
text-shadow:0 0 60px rgba(167,139,250,.4)}
.tagline{color:var(--muted);line-height:1.75;font-size:16px;max-width:520px;margin:0 auto}
.tagline b{color:#4ade80;font-weight:600}
.stats{display:grid;grid-template-columns:repeat(2,1fr);gap:16px;margin:34px 0 30px}
.stat{padding:22px 18px;border-radius:20px;border:1px solid var(--line);
background:rgba(255,255,255,.025);position:relative;overflow:hidden;transition:transform .3s ease}
.stat:hover{transform:translateY(-2px)}
.stat .num{font-family:'JetBrains Mono',monospace;font-size:32px;font-weight:800;
letter-spacing:-.02em;line-height:1}
.stat:first-child .num{background:linear-gradient(135deg,#a78bfa,#22d3ee);
-webkit-background-clip:text;background-clip:text;color:transparent}
.stat:last-child .num{background:linear-gradient(135deg,#4ade80,#22c55e);
-webkit-background-clip:text;background-clip:text;color:transparent}
.stat .lbl{font-size:11px;color:var(--muted);letter-spacing:.18em;
text-transform:uppercase;margin-top:8px;font-weight:600;font-family:'Outfit',sans-serif}
.cta{display:inline-flex;align-items:center;justify-content:center;gap:10px;
width:100%;padding:18px 24px;border-radius:18px;text-decoration:none;
font-family:'Outfit',sans-serif;font-weight:700;font-size:16px;letter-spacing:.03em;
color:#041a0d;background:linear-gradient(135deg,#4ade80,#22c55e);
box-shadow:0 20px 60px rgba(34,197,94,.35),0 0 0 1px rgba(74,222,128,.4) inset;
position:relative;overflow:hidden;transition:transform .2s ease,box-shadow .2s ease}
.cta:hover{transform:translateY(-2px);box-shadow:0 26px 70px rgba(34,197,94,.5),0 0 0 1px rgba(74,222,128,.5) inset}
.cta::before{content:'';position:absolute;top:0;left:-100%;width:100%;height:100%;
background:linear-gradient(90deg,transparent,rgba(255,255,255,.5),transparent);
animation:shine 3s infinite}
@keyframes shine{0%{left:-100%}60%,100%{left:100%}}
.foot{margin-top:22px;font-size:12px;color:#6b6a8a;letter-spacing:.1em;
font-family:'Outfit',sans-serif;text-transform:uppercase;font-weight:500}
.foot .dot{color:var(--purple);margin:0 8px}
@media (max-width:560px){.card{padding:36px 22px 32px;border-radius:24px}
.stats{grid-template-columns:1fr}h1{margin:20px 0 12px}.tagline{font-size:15px}}
</style>
</head>
<body>
<div class="orb orb-1"></div><div class="orb orb-2"></div><div class="orb orb-3"></div>
<div class="grid-bg"></div>
<main class="card">
<span class="badge">Premium Referral Platform</span>
<h1>Cytun<br>Refer Bot</h1>
<p class="tagline">Earn <b>₹2</b> for every successful referral. Join the bot, share your link, and withdraw once you reach <b>₹10</b>.</p>
<section class="stats">
<div class="stat"><div class="num">{{ visits }}</div><div class="lbl">Website Visits</div></div>
<div class="stat"><div class="num">₹2</div><div class="lbl">Per Referral</div></div>
</section>
<a class="cta" href="{{ bot_link }}" target="_blank" rel="noopener">🚀 Open Telegram Bot</a>
<p class="foot">Secure<span class="dot">•</span>Fast<span class="dot">•</span>Premium</p>
</main>
</body>
</html>"""


@app.route("/")
def index():
    seen = request.cookies.get("cytun_seen")
    visits = get_visit_count() if seen else increment_visit()
    bot_link = f"https://t.me/{_bot_username_cache or BOT_USERNAME or 'YOUR_BOT'}"
    html = render_template_string(WEBSITE_HTML, visits=visits, bot_link=bot_link)
    resp = make_response(html)
    if not seen:
        resp.set_cookie("cytun_seen", "1", max_age=60*60*6, httponly=True,
                        samesite="Lax", secure=request.is_secure)
    return resp


# ─────────────────────────────────────────────────────────────────────────────
# 18. STARTUP
# ─────────────────────────────────────────────────────────────────────────────

_ensure_db_dir()
init_db()
_migrate_db()

if __name__ == "__main__":
    ensure_loop()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), threaded=True)
