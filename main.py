
import asyncio
import os
import sqlite3
import secrets
import string
import time
from pathlib import Path

from telethon import TelegramClient, events, Button


# =====================================================
# CONFIG
# =====================================================

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["LICENSE_BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "license_service.db"

bot = TelegramClient(
    str(DATA_DIR / "license_bot"),
    API_ID,
    API_HASH
)

user_states = {}


# =====================================================
# DATABASE
# =====================================================

db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row

db.executescript("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    trial_used INTEGER DEFAULT 0,
    expires_at INTEGER DEFAULT 0,
    enabled INTEGER DEFAULT 1,
    created_at INTEGER
);

CREATE TABLE IF NOT EXISTS admins (
    user_id INTEGER PRIMARY KEY,
    added_at INTEGER
);

CREATE TABLE IF NOT EXISTS codes (
    code TEXT PRIMARY KEY,
    days INTEGER NOT NULL,
    created_by INTEGER,
    used_by INTEGER DEFAULT NULL,
    used_at INTEGER DEFAULT NULL,
    created_at INTEGER
);

CREATE TABLE IF NOT EXISTS purchases (
    user_id INTEGER PRIMARY KEY,
    status TEXT DEFAULT 'pending',
    requested_at INTEGER,
    approved_days INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
""")

db.commit()


# =====================================================
# DATABASE HELPERS
# =====================================================

def now():
    return int(time.time())


def ensure_user(user_id):
    db.execute("""
        INSERT OR IGNORE INTO users
        (user_id, created_at)
        VALUES (?, ?)
    """, (user_id, now()))
    db.commit()


def get_user(user_id):
    ensure_user(user_id)
    return db.execute(
        "SELECT * FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()


def is_owner(user_id):
    return user_id == OWNER_ID


def is_admin(user_id):
    if is_owner(user_id):
        return True

    return db.execute(
        "SELECT 1 FROM admins WHERE user_id=?",
        (user_id,)
    ).fetchone() is not None


def is_global_enabled():
    row = db.execute(
        "SELECT value FROM settings WHERE key='global_enabled'"
    ).fetchone()

    return True if row is None else row["value"] == "1"


def set_global_enabled(value):
    db.execute("""
        INSERT INTO settings(key, value)
        VALUES('global_enabled', ?)
        ON CONFLICT(key)
        DO UPDATE SET value=excluded.value
    """, ("1" if value else "0",))
    db.commit()


def subscription_status(user_id):
    user = get_user(user_id)

    if not is_global_enabled():
        return False, "سرویس موقتاً توسط مالک غیرفعال شده است."

    if not user["enabled"]:
        return False, "دسترسی حساب توسط مدیریت غیرفعال شده است."

    if user["expires_at"] <= now():
        return False, "اشتراک فعال ندارید یا اشتراک شما منقضی شده است."

    remaining = user["expires_at"] - now()
    days = remaining // 86400
    hours = (remaining % 86400) // 3600

    return True, f"اشتراک فعال است؛ {days} روز و {hours} ساعت باقی مانده."


def activate_user(user_id, days):
    if days <= 0:
        raise ValueError("مدت اشتراک باید بیشتر از صفر باشد.")

    ensure_user(user_id)

    user = get_user(user_id)
    start = max(now(), user["expires_at"])

    db.execute("""
        UPDATE users
        SET expires_at=?, enabled=1
        WHERE user_id=?
    """, (start + days * 86400, user_id))

    db.commit()


def create_code(days, creator):
    if not 1 <= days <= 3650:
        raise ValueError("مدت کد باید بین ۱ تا ۳۶۵۰ روز باشد.")

    alphabet = string.ascii_uppercase + string.digits

    while True:
        code = "MAFIA-" + "".join(
            secrets.choice(alphabet) for _ in range(12)
        )

        exists = db.execute(
            "SELECT 1 FROM codes WHERE code=?",
            (code,)
        ).fetchone()

        if not exists:
            break

    db.execute("""
        INSERT INTO codes
        (code, days, created_by, created_at)
        VALUES (?, ?, ?, ?)
    """, (code, days, creator, now()))

    db.commit()
    return code


def redeem_code(user_id, code):
    code = code.strip().upper()

    row = db.execute(
        "SELECT * FROM codes WHERE code=?",
        (code,)
    ).fetchone()

    if row is None:
        return False, "کد واردشده معتبر نیست."

    if row["used_by"] is not None:
        return False, "این کد قبلاً استفاده شده است."

    activate_user(user_id, row["days"])

    db.execute("""
        UPDATE codes
        SET used_by=?, used_at=?
        WHERE code=? AND used_by IS NULL
    """, (user_id, now(), code))

    if db.execute("SELECT changes()").fetchone()[0] != 1:
        db.rollback()
        return False, "این کد هم‌زمان توسط شخص دیگری استفاده شد."

    db.commit()

    return True, f"اشتراک {row['days']} روزه فعال شد."


# =====================================================
# MENUS
# =====================================================

def user_menu():
    return [
        [Button.inline("🎁 دریافت آزمایش رایگان", b"trial")],
        [Button.inline("🔑 وارد کردن کد اشتراک", b"redeem")],
        [Button.inline("💳 خرید اشتراک", b"buy")],
        [Button.inline("📊 وضعیت اشتراک", b"status")]
    ]


def admin_menu():
    return [
        [Button.inline("🎟 ساخت کد اشتراک", b"newcode")],
        [Button.inline("👥 کاربران", b"users")],
        [Button.inline("📋 راهنمای دستورات", b"help")]
    ]


async def show_panel(event):
    uid = event.sender_id

    buttons = user_menu()

    if is_admin(uid):
        buttons += admin_menu()

    if is_owner(uid):
        buttons.append([
            Button.inline("🟢 روشن کردن سرویس", b"global_on"),
            Button.inline("🔴 خاموش کردن سرویس", b"global_off")
        ])

    await event.respond(
        "🏛 MAFIA EMPIRE\n\n"
        "پنل مدیریت اشتراک\n"
        "یکی از گزینه‌های زیر را انتخاب کن:",
        buttons=buttons
    )


# =====================================================
# PRIVATE MESSAGES
# =====================================================

@bot.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
async def message_handler(event):
    uid = event.sender_id
    text = (event.raw_text or "").strip()

    ensure_user(uid)

    # /START
    if text.lower() in ("/start", "/panel"):
        user_states.pop(uid, None)
        await show_panel(event)
        return

    # USER INPUT STATES
    state = user_states.get(uid)

    if state == "redeem":
        user_states.pop(uid, None)

        ok, message = redeem_code(uid, text)

        await event.respond(
            ("✅ " if ok else "❌ ") + message
        )
        return

    if state == "code_days":
        user_states.pop(uid, None)

        if not is_admin(uid):
            return

        try:
            days = int(text)
            code = create_code(days, uid)

            await event.respond(
                f"✅ کد ساخته شد:\n\n"
                f"`{code}`\n\n"
                f"مدت: {days} روز\n"
                f"یک‌بارمصرف است."
            )

        except Exception as error:
            await event.respond(f"❌ خطا: {error}")

        return

    # ADMIN COMMANDS
    if text.startswith("/code "):
        if not is_admin(uid):
            return

        try:
            days = int(text.split()[1])
            code = create_code(days, uid)

            await event.respond(
                f"🎟 کد جدید:\n`{code}`\n"
                f"مدت: {days} روز"
            )
        except Exception:
            await event.respond("فرمت صحیح:\n/code 10")

        return

    if text.startswith("/on ") or text.startswith("/off "):
        if not is_admin(uid):
            return

        try:
            parts = text.split()
            target = int(parts[1])
            enabled = parts[0] == "/on"

            ensure_user(target)

            db.execute(
                "UPDATE users SET enabled=? WHERE user_id=?",
                (int(enabled), target)
            )
            db.commit()

            await event.respond(
                f"وضعیت حساب {target}: "
                f"{'فعال' if enabled else 'غیرفعال'}"
            )

            try:
                await bot.send_message(
                    target,
                    "🟢 دسترسی شما فعال شد."
                    if enabled else
                    "🔴 دسترسی شما توسط مدیریت غیرفعال شد."
                )
            except Exception:
                pass

        except Exception:
            await event.respond("فرمت صحیح:\n/on USER_ID\n/off USER_ID")

        return

    # OWNER COMMANDS
    if text.startswith("/approve "):
        if not is_owner(uid):
            return

        try:
            _, target, days = text.split()
            target = int(target)
            days = int(days)

            activate_user(target, days)

            db.execute("""
                UPDATE purchases
                SET status='approved', approved_days=?
                WHERE user_id=?
            """, (days, target))
            db.commit()

            await event.respond(
                f"✅ اشتراک {target} به مدت {days} روز فعال شد."
            )

            await bot.send_message(
                target,
                f"🎉 درخواست خرید شما تأیید شد.\n"
                f"اشتراک {days} روزه فعال شد."
            )

        except Exception as error:
            await event.respond(
                f"فرمت صحیح:\n/approve USER_ID DAYS\n\n{error}"
            )

        return

    if text.startswith("/reject "):
        if not is_owner(uid):
            return

        try:
            target = int(text.split()[1])

            db.execute("""
                UPDATE purchases SET status='rejected'
                WHERE user_id=?
            """, (target,))
            db.commit()

            await bot.send_message(
                target,
                "❌ درخواست خرید شما رد شد."
            )

            await event.respond("درخواست رد شد.")

        except Exception:
            await event.respond("فرمت صحیح:\n/reject USER_ID")

        return

    if text.startswith("/admin_add "):
        if not is_owner(uid):
            return

        try:
            target = int(text.split()[1])

            db.execute("""
                INSERT OR IGNORE INTO admins(user_id, added_at)
                VALUES (?, ?)
            """, (target, now()))
            db.commit()

            await event.respond(f"✅ {target} ادمین شد.")

        except Exception:
            await event.respond("فرمت صحیح:\n/admin_add USER_ID")

        return

    if text.startswith("/admin_del "):
        if not is_owner(uid):
            return

        try:
            target = int(text.split()[1])

            db.execute(
                "DELETE FROM admins WHERE user_id=?",
                (target,)
            )
            db.commit()

            await event.respond(f"ادمین {target} حذف شد.")

        except Exception:
            await event.respond("فرمت صحیح:\n/admin_del USER_ID")

        return

    if text == "/global_off" and is_owner(uid):
        set_global_enabled(False)
        await event.respond("🔴 سرویس به‌صورت سراسری غیرفعال شد.")
        return

    if text == "/global_on" and is_owner(uid):
        set_global_enabled(True)
        await event.respond("🟢 سرویس فعال شد.")
        return

    if text == "/users" and is_admin(uid):
        rows = db.execute("""
            SELECT user_id, expires_at, enabled
            FROM users ORDER BY created_at DESC LIMIT 100
        """).fetchall()

        if not rows:
            await event.respond("کاربری ثبت نشده است.")
            return

        lines = ["👥 کاربران:\n"]

        for row in rows:
            active = (
                row["enabled"] and row["expires_at"] > now()
            )

            lines.append(
                f"{row['user_id']} | "
                f"{'فعال' if active else 'غیرفعال'}"
            )

        await event.respond("\n".join(lines))
        return


# =====================================================
# BUTTON HANDLER
# =====================================================

@bot.on(events.CallbackQuery)
async def callback_handler(event):
    uid = event.sender_id
    data = event.data.decode("utf-8")

    ensure_user(uid)

    if data == "trial":
        user = get_user(uid)

        if user["trial_used"]:
            await event.answer(
                "آزمایش رایگان قبلاً استفاده شده است.",
                alert=True
            )
            return

        activate_user(uid, 1)

        db.execute(
            "UPDATE users SET trial_used=1 WHERE user_id=?",
            (uid,)
        )
        db.commit()

        await event.answer("آزمایش یک‌روزه فعال شد.", alert=True)
        await event.respond("🎁 اشتراک آزمایشی یک‌روزه فعال شد.")
        return

    if data == "redeem":
        user_states[uid] = "redeem"
        await event.respond("🔑 کد اشتراک را ارسال کن:")
        return

    if data == "buy":
        db.execute("""
            INSERT INTO purchases(user_id, status, requested_at)
            VALUES (?, 'pending', ?)
            ON CONFLICT(user_id)
            DO UPDATE SET status='pending', requested_at=excluded.requested_at
        """, (uid, now()))
        db.commit()

        await event.respond(
            "📨 درخواست خرید شما برای مالک ارسال شد."
        )

        try:
            await bot.send_message(
                OWNER_ID,
                f"💳 درخواست خرید جدید\n\n"
                f"شناسه کاربر: `{uid}`\n\n"
                f"برای تأیید:\n"
                f"/approve {uid} 30\n\n"
                f"برای رد:\n"
                f"/reject {uid}"
            )
        except Exception:
            pass

        return

    if data == "status":
        active, message = subscription_status(uid)
        await event.respond(
            ("🟢 " if active else "🔴 ") + message
        )
        return

    if data == "newcode":
        if not is_admin(uid):
            await event.answer("دسترسی ندارید.", alert=True)
            return

        user_states[uid] = "code_days"
        await event.respond(
            "مدت کد را به روز وارد کن.\nمثال: 10"
        )
        return

    if data == "users":
        if not is_admin(uid):
            return

        rows = db.execute("""
            SELECT user_id, expires_at, enabled
            FROM users ORDER BY created_at DESC LIMIT 100
        """).fetchall()

        lines = ["👥 فهرست کاربران:\n"]

        for row in rows:
            active = (
                row["enabled"] and row["expires_at"] > now()
            )

            lines.append(
                f"{row['user_id']} — "
                f"{'فعال' if active else 'غیرفعال'}"
            )

        await event.respond("\n".join(lines))
        return

    if data == "help":
        if not is_admin(uid):
            return

        await event.respond(
            "دستورات مدیریت:\n\n"
            "/code 10 — ساخت کد ۱۰ روزه\n"
            "/on USER_ID — فعال‌سازی حساب\n"
            "/off USER_ID — غیرفعال‌سازی حساب\n"
            "/users — فهرست کاربران\n\n"
            "دستورات مخصوص مالک:\n"
            "/approve USER_ID DAYS\n"
            "/reject USER_ID\n"
            "/admin_add USER_ID\n"
            "/admin_del USER_ID\n"
            "/global_on\n"
            "/global_off"
        )
        return

    if data == "global_on" and is_owner(uid):
        set_global_enabled(True)
        await event.respond("🟢 سرویس فعال شد.")
        return

    if data == "global_off" and is_owner(uid):
        set_global_enabled(False)
        await event.respond("🔴 سرویس خاموش شد.")
        return

    await event.answer("دستور نامعتبر است.", alert=True)


# =====================================================
# MAIN
# =====================================================

async def main():
    await bot.start(bot_token=BOT_TOKEN)

    print("MAFIA LICENSE SERVICE STARTED")
    print("OWNER:", OWNER_ID)
    print("DATABASE:", DB_PATH)

    await bot.send_message(
        OWNER_ID,
        "✅ سیستم مدیریت اشتراک فعال شد."
    )

    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
