import asyncio
import contextvars
import shutil
import json
import base64
import os
import re
import random
import time
import math
from datetime import datetime
from pathlib import Path

from telethon import TelegramClient, events, connection, Button
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PasswordHashInvalidError,
    PhoneNumberInvalidError,
    FloodWaitError
)


# =========================================================
# 1. TELEGRAM ACCOUNT  (از Environment Variable خوانده می‌شود)
# =========================================================

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])  # آیدی عددی خودت

# مسیر Volume در Railway
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)



# =========================================================
# 2. CLIENTS
# =========================================================

# اکانت فعلی در هر Handler / Task (هر اکانت کاملاً مستقل است)
CUR = contextvars.ContextVar("current_account")

ACCOUNTS_DIR = DATA_DIR / "accounts"
ACCOUNTS_FILE = DATA_DIR / "accounts.json"
ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)

accounts_meta = {}   # id -> {"name", "user_id", "enabled", "expires_at", "created"}
accounts = {}        # id -> Account


class Account:
    """همه‌ی وضعیت‌های یک اکانت (کلاینت، تنظیمات، تسک‌ها، تاریخچه قیمت، ...)."""

    def __init__(self, account_id, client=None):

        self.id = account_id
        self.dir = ACCOUNTS_DIR / account_id
        self.dir.mkdir(parents=True, exist_ok=True)

        self.client = client or TelegramClient(
            str(self.dir / "session"),
            API_ID,
            API_HASH
        )

        self.authorized = False
        self.handlers_attached = False

        self.settings = {}
        self.tasks = {}
        self.locks = {}
        self.interval_events = {}
        self.price_history = {}
        self.price_learning_state = {}
        self.battle_settings = {}
        self.battle_tasks = {}
        self.battle_settings_loaded = False

    @property
    def meta(self):

        return accounts_meta.get(self.id, {})

    def is_active(self):

        meta = self.meta

        if not meta.get("enabled", False):
            return False

        expires_at = meta.get("expires_at")

        return (not expires_at) or time.time() < expires_at

    def stop_tasks(self):

        for group in (self.tasks, self.battle_tasks):

            for task in list(group.values()):

                if not task.done():
                    task.cancel()


class _AccountDict:
    """دیکشنری‌ای که همیشه به اکانتِ فعلیِ همان Handler/Task اشاره می‌کند."""

    def __init__(self, attribute):

        self._attribute = attribute

    def _d(self):

        return getattr(CUR.get(), self._attribute)

    def __getitem__(self, key):
        return self._d()[key]

    def __setitem__(self, key, value):
        self._d()[key] = value

    def __contains__(self, key):
        return key in self._d()

    def __iter__(self):
        return iter(self._d())

    def __len__(self):
        return len(self._d())

    def __getattr__(self, name):
        return getattr(self._d(), name)


class _AccountClient:
    """کلاینت تلگرامِ اکانتِ فعلی؛ کدهای قبلی بدون تغییر از `client` استفاده می‌کنند."""

    def __getattr__(self, name):

        return getattr(CUR.get().client, name)


client = _AccountClient()

# ربات لاگین
bot = TelegramClient(
    str(DATA_DIR / "login_bot"),
    API_ID,
    API_HASH
)


# =========================================================
# 3. CONFIGURATION
# =========================================================

DEFAULT_SETTINGS = {
    "product": "نام محصول",
    "min_price": 300000,
    "warehouse_capacity": 350,
    "interval": 1800
}

settings = _AccountDict("settings")
tasks = _AccountDict("tasks")
locks = _AccountDict("locks")
interval_events = _AccountDict("interval_events")


def acc_file(name):

    return CUR.get().dir / name


def load_settings():

    account = CUR.get()
    config_file = acc_file("mafia_settings.json")

    if not config_file.exists():
        account.settings = {}
        return

    try:

        loaded = json.loads(
            config_file.read_text(encoding="utf-8")
        )

        account.settings = loaded if isinstance(loaded, dict) else {}

    except Exception as error:

        print("Config loading error:", repr(error))
        account.settings = {}


def save_settings():

    acc_file("mafia_settings.json").write_text(
        json.dumps(
            settings._d(),
            ensure_ascii=False,
            indent=4
        ),
        encoding="utf-8"
    )


def get_settings(chat_id):

    key = str(chat_id)

    if key not in settings:

        settings[key] = DEFAULT_SETTINGS.copy()
        save_settings()

    else:

        for name, value in DEFAULT_SETTINGS.items():
            settings[key].setdefault(name, value)

    return settings[key]


def get_lock(chat_id):

    if chat_id not in locks:
        locks[chat_id] = asyncio.Lock()

    return locks[chat_id]


def get_interval_event(chat_id):

    if chat_id not in interval_events:
        interval_events[chat_id] = asyncio.Event()

    return interval_events[chat_id]


# =========================================================
# 4. TEXT UTILITIES
# =========================================================

def normalize_digits(text):

    table = str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
        "01234567890123456789"
    )

    return str(text).translate(table)


def clean_number(value):

    value = normalize_digits(str(value))

    digits = re.sub(r"\D", "", value)

    if not digits:
        raise ValueError("عدد معتبر نیست.")

    return int(digits)


def get_warehouse(text):

    text = normalize_digits(text or "")

    match = re.search(
        r"انبار\s*:\s*([\d,٬]+)\s*/\s*([\d,٬]+)",
        text
    )

    if not match:
        return None

    return (
        clean_number(match.group(1)),
        clean_number(match.group(2))
    )


def get_price(text):

    text = normalize_digits(text or "")

    match = re.search(
        r"قیمت\s*الان\s*[:：]\s*`?([\d,٬،]+)",
        text
    )

    if not match:
        return None

    return clean_number(match.group(1))


def normalize_market_line(text):

    text = normalize_digits(text or "")

    for character in (
        "\u200e",
        "\u200f",
        "\u200b",
        "\ufeff",
        "\u2060"
    ):
        text = text.replace(character, "")

    return text.replace("*", "").replace("`", "").strip()


def get_market_product_price(text, product):

    wanted_product = normalize_market_line(
        product
    ).casefold()

    if not wanted_product:
        return None

    lines = [
        normalize_market_line(line)
        for line in (text or "").splitlines()
    ]

    for index, line in enumerate(lines):

        if wanted_product not in line.casefold():
            continue

        for following_line in lines[index + 1:]:

            if not following_line:
                continue

            return get_price(following_line)

        return None

    return None


# =========================================================
# 4.1. ADAPTIVE ONLINE PRICE LEARNING
# =========================================================

PRICE_HISTORY_LIMIT = 1000
PRICE_INTERVAL_SECONDS = 3600
PRICE_MIN_SAMPLE_GAP = 3300
PRICE_RETURN_LIMIT = 0.08

price_history = _AccountDict("price_history")
price_learning_state = _AccountDict("price_learning_state")


def load_price_history():

    account = CUR.get()
    history_file = acc_file("mafia_price_history.json")
    state_file = acc_file("mafia_price_learning_state.json")

    try:
        if history_file.exists():
            loaded = json.loads(history_file.read_text(encoding="utf-8"))
            account.price_history = loaded if isinstance(loaded, dict) else {}
        else:
            account.price_history = {}
    except Exception as error:
        print("[PRICE HISTORY LOAD ERROR]", repr(error))
        account.price_history = {}

    try:
        if state_file.exists():
            loaded = json.loads(state_file.read_text(encoding="utf-8"))
            account.price_learning_state = loaded if isinstance(loaded, dict) else {}
        else:
            account.price_learning_state = {}
    except Exception as error:
        print("[PRICE STATE LOAD ERROR]", repr(error))
        account.price_learning_state = {}


def save_price_history():

    path = acc_file("mafia_price_history.json")
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(price_history._d(), ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    temporary.replace(path)


def save_price_learning_state():

    path = acc_file("mafia_price_learning_state.json")
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(price_learning_state._d(), ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    temporary.replace(path)


def get_price_history_key(chat_id, product):

    return f"{chat_id}:{str(product).strip().casefold()}"


def _valid_price_records(records):

    result = []
    for item in records:
        try:
            timestamp = int(item["timestamp"])
            price = int(item["price"])
            if timestamp > 0 and price > 0:
                result.append({"timestamp": timestamp, "price": price})
        except (KeyError, TypeError, ValueError):
            continue
    result.sort(key=lambda item: item["timestamp"])
    return result


def _median(values):

    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _candidate_returns(prices):

    """Return several deliberately conservative one-hour forecast candidates."""

    if len(prices) < 2:
        return {"ثبات قیمت": 0.0}

    returns = [
        math.log(prices[index] / prices[index - 1])
        for index in range(1, len(prices))
        if prices[index - 1] > 0 and prices[index] > 0
    ]

    if not returns:
        return {"ثبات قیمت": 0.0}

    recent3 = returns[-3:]
    recent5 = returns[-5:]

    # Exponentially weighted recent movement.
    ewma = returns[0]
    alpha = 0.45
    for value in returns[1:]:
        ewma = alpha * value + (1.0 - alpha) * ewma

    # Robust short trend from the latest price levels.
    trend = 0.0
    if len(prices) >= 3:
        log_prices = [math.log(value) for value in prices[-8:]]
        x_mean = (len(log_prices) - 1) / 2.0
        y_mean = sum(log_prices) / len(log_prices)
        denominator = sum((i - x_mean) ** 2 for i in range(len(log_prices)))
        if denominator:
            trend = sum(
                (i - x_mean) * (value - y_mean)
                for i, value in enumerate(log_prices)
            ) / denominator

    return {
        "ثبات قیمت": 0.0,
        "میانه تغییرات اخیر": 0.60 * _median(recent5),
        "میانگین کوتاه‌مدت": 0.45 * (sum(recent3) / len(recent3)),
        "میانگین نمایی": 0.60 * ewma,
        "روند خطی تعدیل‌شده": 0.50 * trend,
    }


def _adaptive_forecast_return(prices):

    candidates = _candidate_returns(prices)
    names = list(candidates)

    # Walk-forward validation: each historical point is predicted only
    # from observations that would have been available at that time.
    errors = {name: [] for name in names}
    if len(prices) >= 5:
        for end in range(3, len(prices)):
            historical = prices[:end]
            actual_return = math.log(prices[end] / prices[end - 1])
            historical_candidates = _candidate_returns(historical)
            for name in names:
                predicted = historical_candidates.get(name, 0.0)
                errors[name].append(abs(predicted - actual_return))

    if any(errors.values()):
        # Lower historical error earns more weight; smoothing prevents
        # a single lucky forecast from dominating the ensemble.
        inverse = {
            name: 1.0 / (0.005 + (sum(values) / len(values) if values else 0.03))
            for name, values in errors.items()
        }
        total = sum(inverse.values()) or 1.0
        weights = {name: value / total for name, value in inverse.items()}
    else:
        weights = {name: 1.0 / len(names) for name in names}

    forecast = sum(candidates[name] * weights[name] for name in names)
    forecast = max(-PRICE_RETURN_LIMIT, min(PRICE_RETURN_LIMIT, forecast))

    all_errors = [value for values in errors.values() for value in values]
    validation_error = (sum(all_errors) / len(all_errors)) if all_errors else None

    return forecast, weights, validation_error


def predict_product_price_one_hour(chat_id, product, interval_seconds=3600):

    key = get_price_history_key(chat_id, product)
    records = _valid_price_records(price_history.get(key, []))
    prices = [item["price"] for item in records]

    if not prices:
        return {"ready": False, "samples": 0, "required": 1}

    current_price = prices[-1]
    predicted_return, weights, validation_error = _adaptive_forecast_return(prices)
    predicted_price = max(1, round(current_price * math.exp(predicted_return)))
    change_percent = ((predicted_price / current_price) - 1.0) * 100.0

    if change_percent > 0.15:
        trend = "صعودی"
    elif change_percent < -0.15:
        trend = "نزولی"
    else:
        trend = "تقریباً ثابت"

    if len(prices) == 1:
        confidence = "آغاز یادگیری؛ داده قبلی وجود ندارد"
    elif validation_error is None:
        confidence = "در حال یادگیری"
    elif validation_error < 0.01:
        confidence = "خطای تاریخی کم"
    elif validation_error < 0.03:
        confidence = "خطای تاریخی متوسط"
    else:
        confidence = "خطای تاریخی زیاد"

    return {
        "ready": True,
        "current": current_price,
        "predicted": predicted_price,
        "change_percent": change_percent,
        "trend": trend,
        "confidence": confidence,
        "samples": len(prices),
        "forecast_steps": 1,
        "weights": weights,
        "validation_error": validation_error,
    }


def format_price_prediction(chat_id, product, interval_seconds=3600):

    result = predict_product_price_one_hour(chat_id, product, interval_seconds)
    if not result["ready"]:
        return "🧠 مغز یادگیرنده قیمت\n━━━━━━━━━━━━━━━━━━\nهنوز اولین قیمت ثبت نشده است؛ با اولین بررسی واقعی، پیش‌بینی آغاز می‌شود."

    change_sign = "+" if result["change_percent"] >= 0 else ""
    return (
        "🧠 پیش‌بینی تطبیقی قیمت\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"محصول: {product}\n"
        f"قیمت مبنا: {result['current']:,}\n"
        f"پیش‌بینی یک ساعت آینده: {result['predicted']:,}\n"
        f"تغییر احتمالی: {change_sign}{result['change_percent']:.2f}%\n"
        f"روند تخمینی: {result['trend']}\n"
        f"وضعیت یادگیری: {result['confidence']}\n"
        f"نمونه‌های ساعتی: {result['samples']}\n"
        "مدل با خطاهای قبلی وزن پیش‌بینی‌کننده‌ها را تنظیم می‌کند.\n"
        "⚠️ پیش‌بینی تخمینی است و تضمین قیمت واقعی نیست."
    )


def update_price_learning_and_report(chat_id, product, price):

    """Score the due one-hour forecast, learn from the new hourly point,
    create the next forecast, persist everything, and return a DM report."""

    key = get_price_history_key(chat_id, product)
    now = int(time.time())
    records = _valid_price_records(price_history.get(key, []))
    state = price_learning_state.setdefault(key, {"pending": None, "evaluations": []})
    pending = state.get("pending")
    accuracy_text = "هنوز پیش‌بینی قبلی به افق یک‌ساعته نرسیده است."

    if pending and now >= int(pending.get("target_timestamp", now + 1)):
        predicted = int(pending.get("predicted_price", price))
        actual = int(price)
        accuracy = max(0.0, 100.0 * (1.0 - abs(predicted - actual) / max(actual, 1)))
        evaluation = {
            "timestamp": now,
            "predicted": predicted,
            "actual": actual,
            "accuracy": round(accuracy, 4),
            "absolute_error": abs(predicted - actual),
        }
        state.setdefault("evaluations", []).append(evaluation)
        state["evaluations"] = state["evaluations"][-500:]
        state["pending"] = None
        accuracy_text = (
            "📊 ارزیابی پیش‌بینی یک ساعت قبل\n"
            f"پیش‌بینی قبلی: {predicted:,}\n"
            f"قیمت واقعی: {actual:,}\n"
            f"درصد نزدیکی: {accuracy:.2f}%\n"
            f"اختلاف قیمت: {abs(predicted - actual):,}"
        )

    # The game price is hourly. Ignore repeated polling within the same hour
    # so 30-minute checks do not train the model on duplicate observations.
    should_add = (
        not records
        or now - records[-1]["timestamp"] >= PRICE_MIN_SAMPLE_GAP
    )

    # Migrate gracefully from the previous script version: if old history
    # exists but no pending forecast was saved, start forecasting immediately.
    if not pending and records and not should_add:
        result = predict_product_price_one_hour(chat_id, product, 3600)
        predicted = result["predicted"] if result["ready"] else int(price)
        state["pending"] = {
            "issued_timestamp": now,
            "target_timestamp": now + PRICE_INTERVAL_SECONDS,
            "base_price": int(price),
            "predicted_price": int(predicted),
        }
        save_price_learning_state()
        pending = state["pending"]

    if should_add:
        records.append({"timestamp": now, "price": int(price)})
        price_history[key] = records[-PRICE_HISTORY_LIMIT:]
        save_price_history()

        result = predict_product_price_one_hour(chat_id, product, 3600)
        predicted = result["predicted"] if result["ready"] else int(price)
        state["pending"] = {
            "issued_timestamp": now,
            "target_timestamp": now + PRICE_INTERVAL_SECONDS,
            "base_price": int(price),
            "predicted_price": int(predicted),
        }
        save_price_learning_state()

        return (
            f"{accuracy_text}\n\n"
            "🧠 پیش‌بینی جدید برای یک ساعت آینده\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"محصول: {product}\n"
            f"قیمت فعلی: {int(price):,}\n"
            f"پیش‌بینی یک ساعت آینده: {int(predicted):,}\n"
            f"تعداد نمونه‌های ساعتی: {len(price_history[key])}\n"
            "مدل پس از دریافت قیمت واقعی بعدی، درصد نزدیکی را محاسبه و وزن‌هایش را به‌روزرسانی می‌کند."
        )

    # If the next one-hour observation has not arrived yet, keep the existing
    # forecast intact rather than resetting its evaluation deadline.
    save_price_learning_state()
    return (
        f"🧠 پایش قیمت محصول: {product}\n"
        f"قیمت مشاهده‌شده فعلی: {int(price):,}\n"
        f"پیش‌بینی فعال برای موعد یک‌ساعته: {int((pending or {}).get('predicted_price', price)):,}\n"
        f"{accuracy_text}\n"
        "هنوز یک ساعت کامل از آخرین نمونه نگذشته؛ پیش‌بینی فعال تغییر داده نشد."
    )


# =========================================================
# 5. FACTORY MESSAGE DETECTION
# =========================================================

def is_factory_message(text):

    if not text:
        return False

    text = normalize_digits(text)

    patterns = [
        r"مدیر\s*:",
        r"انبار\s*:",
        r"نیرو\s*:",
        r"دستگاه\s*:",
        r"سرکارگر\s*:"
    ]

    matched = sum(
        bool(re.search(pattern, text))
        for pattern in patterns
    )

    return (
        bool(re.search(r"انبار\s*:", text))
        and matched >= 3
    )


async def send_factory_and_wait(chat_id, timeout=60):

    loop = asyncio.get_running_loop()
    future = loop.create_future()

    command_message = None

    async def check_factory(event):

        if future.done():
            return

        if command_message is None:
            return

        message = event.message

        if message.chat_id != chat_id:
            return

        if message.reply_to_msg_id != command_message.id:
            return

        text = message.raw_text or ""

        if not is_factory_message(text):
            return

        if not message.buttons:
            return

        print("[FACTORY] Valid direct reply detected")
        print("[FACTORY] Reply ID:", message.id)

        future.set_result(message)

    new_builder = events.NewMessage(chats=chat_id)
    edit_builder = events.MessageEdited(chats=chat_id)

    client.add_event_handler(check_factory, new_builder)
    client.add_event_handler(check_factory, edit_builder)

    try:

        print("[1] Sending factory command...")

        command_message = await client.send_message(
            chat_id,
            "کارخونه"
        )

        print("[1] Factory command ID:", command_message.id)
        print("[1] Waiting for direct reply...")

        result = await asyncio.wait_for(
            future,
            timeout=timeout
        )

        return result

    finally:

        client.remove_event_handler(
            check_factory,
            new_builder
        )

        client.remove_event_handler(
            check_factory,
            edit_builder
        )


# =========================================================
# 6. MARKET MESSAGE DETECTION
# =========================================================




async def wait_for_market(
    chat_id,
    product,
    factory_message_id,
    timeout=60
):

    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def is_valid_market(message):

        text = message.raw_text or ""

        if get_market_product_price(text, product) is None:
            return False

        if not message.buttons:
            return False

        return True

    async def check_market(event):

        if future.done():
            return

        message = event.message

        if message.chat_id != chat_id:
            return

        if message.id < factory_message_id:
            return

        if not is_valid_market(message):
            return

        if future.done():
            return

        print("[MARKET] Valid market panel detected")
        print("[MARKET] Message ID:", message.id)

        future.set_result(message)

    new_builder = events.NewMessage(chats=chat_id)
    edit_builder = events.MessageEdited(chats=chat_id)

    client.add_event_handler(check_market, new_builder)
    client.add_event_handler(check_market, edit_builder)

    try:

        print("[4] Waiting for market panel...")

        recent_messages = await client.get_messages(
            chat_id,
            limit=20
        )

        for message in recent_messages:

            # اگر رویداد تلگرام قبلاً پیام را پیدا کرده،
            # دیگر Future را دوباره تکمیل نکن.
            if future.done():
                break

            if message.id < factory_message_id:
                continue

            if is_valid_market(message):

                if not future.done():

                    print("[MARKET] Existing market panel detected")
                    print("[MARKET] Message ID:", message.id)

                    future.set_result(message)

                break

        result = await asyncio.wait_for(
            future,
            timeout=timeout
        )

        return result

    finally:

        client.remove_event_handler(
            check_market,
            new_builder
        )

        client.remove_event_handler(
            check_market,
            edit_builder
        )



# =========================================================
# 7. BUTTON UTILITIES
# =========================================================

def flatten_buttons(message):

    result = []

    if not message.buttons:
        return result

    for row_index, row in enumerate(message.buttons):

        for column_index, button in enumerate(row):

            result.append(
                (row_index, column_index, button)
            )

    return result


async def click_market_button(message):

    if not message.buttons:
        raise RuntimeError(
            "پیام کارخانه هیچ دکمه‌ای ندارد."
        )

    # اولین ردیف، آخرین ستون
    row = 0
    column = len(message.buttons[0]) - 1

    button = message.buttons[row][column]

    print(
        "[3] Market button:",
        getattr(button, "text", "Unknown")
    )

    await message.click(row, column)

    print("[3] Market button clicked successfully")

async def click_final_button(message):

    buttons = flatten_buttons(message)

    if len(buttons) < 3:
        raise RuntimeError(
            "کمتر از سه دکمه در پنل بازار وجود دارد."
        )

    # دقیقاً همان روش نسخه اولیه
    row, column, button = buttons[-3]

    print(
        "[6] Final button:",
        getattr(button, "text", "Unknown")
    )

    await message.click(row, column)

    print("[6] Final button clicked successfully")



# =========================================================
# 8. FACTORY CYCLE
# =========================================================

async def run_cycle(chat_id, test=False):

    config = get_settings(chat_id)

    async with get_lock(chat_id):

        print("\n" + "=" * 45)
        print("NEW CYCLE")
        print("TEST MODE:", test)
        print("=" * 45)

        if test:

            await send_report(
                chat_id,
                "🧪 تست کامل شروع شد.\n"
                "شرط انبار و قیمت نادیده گرفته می‌شوند.\n"
                "توجه: کلیک نهایی هم انجام خواهد شد."
            )

        # ---------------------------------------------
        # STEP 1 - FACTORY
        # ---------------------------------------------

        print("\n[1] FACTORY")

        factory_message = await send_factory_and_wait(
            chat_id,
            timeout=60
        )

        print("[OK] Factory message received")

        # ---------------------------------------------
        # STEP 2 - WAREHOUSE
        # ---------------------------------------------

        print("\n[2] WAREHOUSE")

        warehouse = get_warehouse(
            factory_message.raw_text or ""
        )

        if warehouse is None:

            raise RuntimeError(
                "اطلاعات انبار قابل استخراج نیست."
            )

        current, maximum = warehouse

        print(
            f"Warehouse: {current}/{maximum}"
        )

        if not test:

            required = config["warehouse_capacity"]

            if (
                current != required
                or maximum != required
            ):

                await send_report(
                    chat_id,
                    f"⏸ انبار کامل نیست.\n"
                    f"مقدار فعلی: {current}/{maximum}\n"
                    f"ظرفیت مورد انتظار: {required}\n"
                    f"{config['interval'] // 60} دقیقه دیگر دوباره بررسی می‌کنم."
                )

                return False

        # ---------------------------------------------
        # STEP 3 - OPEN MARKET
        # ---------------------------------------------

        print("\n[3] OPEN MARKET")

        await click_market_button(
            factory_message
        )

        # ---------------------------------------------
        # STEP 4 - FIND PRODUCT
        # ---------------------------------------------

        product = str(config["product"]).strip()

        if not product:

            raise RuntimeError(
                "نام محصول در تنظیمات خالی است."
            )

        print("\n[4] FIND PRODUCT:", product)

        market_message = await wait_for_market(
            chat_id=chat_id,
            product=product,
            factory_message_id=factory_message.id,
            timeout=60
        )

        # ---------------------------------------------
        # STEP 5 - CHECK PRICE
        # ---------------------------------------------

        print("\n[5] CHECK PRICE")

        price = get_market_product_price(
            market_message.raw_text or "",
            product
        )

        if price is None:

            raise RuntimeError(
                "قیمت محصول قابل تشخیص نیست."
            )

        print(
            f"Current price: {price:,}"
        )

        # Test runs do not pollute the model's training history.
        if not test:

            prediction_report = update_price_learning_and_report(
                chat_id,
                product,
                price
            )

            await send_report(
                chat_id,
                prediction_report
            )

        if not test:

            minimum = config["min_price"]

            if price < minimum:

                await send_report(
                    chat_id,
                    f"⏸ قیمت مناسب نیست.\n"
                    f"قیمت فعلی: {price:,}\n"
                    f"حداقل قیمت: {minimum:,}\n"
                    f"{config['interval'] // 60} دقیقه دیگر بررسی می‌کنم."
                )

                return False

        # ---------------------------------------------
        # STEP 6 - FINAL BUTTON
        # ---------------------------------------------

        print("\n[6] FINAL BUTTON")

        await click_final_button(
            market_message
        )

        # ---------------------------------------------
        # RESULT
        # ---------------------------------------------

        if test:

            await send_report(
                chat_id,
                "🧪 مراحل تست اجرا شدند.\n"
                "کلیک نهایی نیز ارسال شد."
            )

        else:

            await send_report(
                chat_id,
                f"✅ عملیات انجام شد.\n"
                f"محصول: {product}\n"
                f"قیمت: {price:,}"
            )

        print("\nCYCLE COMPLETED")

        return True


# =========================================================
# 9. AUTOMATIC LOOP
# =========================================================

async def automatic_loop(chat_id):

    print(
        f"[AUTO] Started for group {chat_id}"
    )

    while True:

        try:

            await run_cycle(
                chat_id,
                test=False
            )

        except asyncio.CancelledError:

            print("[AUTO] Stop requested")

            await send_report(
                chat_id,
                "⏹ مدیریت خودکار کارخانه متوقف شد."
            )

            raise

        except Exception as error:

            print(
                "[AUTO ERROR]",
                repr(error)
            )

            await send_report(
                chat_id,
                f"⚠️ خطا در اجرای عملیات:\n"
                f"{error}\n\n"
                f"در تلاش بعدی دوباره بررسی می‌کنم."
            )

        interval_event = get_interval_event(chat_id)

        while True:

            interval_event.clear()

            interval = get_settings(chat_id)["interval"]

            print(
                f"[AUTO] Next cycle in {interval // 60} minutes"
            )

            try:

                await asyncio.wait_for(
                    interval_event.wait(),
                    timeout=interval
                )

                print(
                    "[AUTO] Interval changed; restarting countdown"
                )

            except asyncio.TimeoutError:

                break


# =========================================================
# 10. TEST
# =========================================================

async def test_once(chat_id):

    try:

        await run_cycle(
            chat_id,
            test=True
        )

    except asyncio.CancelledError:

        raise

    except Exception as error:

        print(
            "[TEST ERROR]",
            repr(error)
        )

        await send_report(
            chat_id,
            f"❌ تست ناموفق بود:\n{error}"
        )


# =========================================================
# COMMAND UTILITIES
# =========================================================

async def send_report(chat_id, message):

    try:

        await client.send_message(
            "me",
            f"📍 گروه: `{chat_id}`\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"{message}"
        )

    except Exception as error:

        print(
            "[REPORT ERROR]",
            repr(error)
        )


def schedule_command_delete(event):

    async def delete_command():

        await asyncio.sleep(0.5)

        try:

            await event.delete()

        except Exception as error:

            print(
                "[COMMAND DELETE ERROR]",
                repr(error)
            )

    asyncio.create_task(delete_command())


# =========================================================
# 11. COMMAND HANDLER
# =========================================================

COMMAND_PATTERN = (
    r"(?i)^/(GO|OFF|TEST|INFO|P|MIN|CAP|TIME|PREDICT|HELP|START|STOP|STATUS|PRODUCT|PRICE|CAPACITY|INTERVAL)(?:\s+.*)?$"
)


async def command_handler(event):

    if not event.is_group:
        return

    raw_text = (event.raw_text or "").strip()

    if not raw_text:
        return

    parts = raw_text.split(maxsplit=1)

    command = parts[0].upper()

    argument = (
        parts[1].strip()
        if len(parts) > 1
        else ""
    )

    aliases = {
        "/GO": "/START",
        "/OFF": "/STOP",
        "/INFO": "/STATUS",
        "/P": "/PRODUCT",
        "/MIN": "/PRICE",
        "/CAP": "/CAPACITY",
        "/TIME": "/INTERVAL"
    }

    command = aliases.get(command, command)

    valid_commands = {
        "/START",
        "/STOP",
        "/TEST",
        "/STATUS",
        "/PRODUCT",
        "/PRICE",
        "/CAPACITY",
        "/INTERVAL",
        "/PREDICT",
        "/HELP"
    }

    if command not in valid_commands:
        return

    chat_id = event.chat_id

    schedule_command_delete(event)

    config = get_settings(chat_id)

    # -----------------------------------------------------
    # START
    # -----------------------------------------------------

    if command == "/START":

        task = tasks.get(chat_id)

        if task and not task.done():

            await send_report(
                chat_id,
                "⚠️ مدیریت خودکار از قبل فعال است."
            )

            return

        tasks[chat_id] = asyncio.create_task(
            automatic_loop(chat_id)
        )

        await send_report(
            chat_id,
            "🚀 مدیریت خودکار فعال شد."
        )

    # -----------------------------------------------------
    # STOP
    # -----------------------------------------------------

    elif command == "/STOP":

        task = tasks.get(chat_id)

        if task and not task.done():

            task.cancel()

            await send_report(
                chat_id,
                "⏹ درخواست توقف ارسال شد."
            )

        else:

            await send_report(
                chat_id,
                "مدیریت خودکار فعال نیست."
            )

    # -----------------------------------------------------
    # TEST
    # -----------------------------------------------------

    elif command == "/TEST":

        task = tasks.get(chat_id)

        if task and not task.done():

            await send_report(
                chat_id,
                "⚠️ ابتدا /OFF را اجرا کن."
            )

            return

        tasks[chat_id] = asyncio.create_task(
            test_once(chat_id)
        )

        await send_report(
            chat_id,
            "🧪 تست کامل آغاز شد.\n"
            "توجه: این تست کلیک نهایی را هم انجام می‌دهد."
        )

    # -----------------------------------------------------
    # STATUS
    # -----------------------------------------------------

    elif command == "/STATUS":

        task = tasks.get(chat_id)

        running = (
            task is not None
            and not task.done()
        )

        await send_report(
            chat_id,
            f"📊 وضعیت: "
            f"{'فعال' if running else 'متوقف'}\n\n"
            f"محصول: {config['product']}\n"
            f"حداقل قیمت: {config['min_price']:,}\n"
            f"ظرفیت انبار: "
            f"{config['warehouse_capacity']}\n"
            f"فاصله بررسی: "
            f"{config['interval'] // 60} دقیقه"
        )

    # -----------------------------------------------------
    # PRODUCT
    # -----------------------------------------------------

    elif command == "/PRODUCT":

        if not argument:

            await send_report(
                chat_id,
                "فرمت صحیح:\n/P نام محصول"
            )

            return

        config["product"] = argument

        save_settings()

        await send_report(
            chat_id,
            f"✅ محصول تنظیم شد:\n{argument}"
        )

    # -----------------------------------------------------
    # PRICE
    # -----------------------------------------------------

    elif command == "/PRICE":

        try:

            value = clean_number(argument)

            if value < 0:
                raise ValueError

            config["min_price"] = value

            save_settings()

            await send_report(
                chat_id,
                f"✅ حداقل قیمت تنظیم شد:\n{value:,}"
            )

        except ValueError:

            await send_report(
                chat_id,
                "فرمت صحیح:\n/MIN 393400"
            )

    # -----------------------------------------------------
    # CAPACITY
    # -----------------------------------------------------

    elif command == "/CAPACITY":

        try:

            value = clean_number(argument)

            if value <= 0:
                raise ValueError

            config["warehouse_capacity"] = value

            save_settings()

            await send_report(
                chat_id,
                f"✅ ظرفیت انبار تنظیم شد:\n{value}"
            )

        except ValueError:

            await send_report(
                chat_id,
                "فرمت صحیح:\n/CAP 350"
            )

    # -----------------------------------------------------
    # INTERVAL
    # -----------------------------------------------------

    elif command == "/INTERVAL":

        try:

            value = clean_number(argument)

            if value <= 0:
                raise ValueError

            config["interval"] = value * 60

            save_settings()

            get_interval_event(chat_id).set()

            await send_report(
                chat_id,
                f"✅ فاصله بررسی تنظیم شد:\n"
                f"{value} دقیقه"
            )

        except ValueError:

            await send_report(
                chat_id,
                "فرمت صحیح:\n/TIME 30"
            )

    # -----------------------------------------------------
    # PRICE PREDICTION
    # -----------------------------------------------------

    elif command == "/PREDICT":

        product = str(config["product"]).strip()

        if not product or product == "نام محصول":

            await send_report(
                chat_id,
                "⚠️ ابتدا محصول را تنظیم کن:\n/P نام محصول"
            )

            return

        await send_report(
            chat_id,
            format_price_prediction(
                chat_id,
                product,
                config["interval"]
            )
        )

    # -----------------------------------------------------
    # HELP
    # -----------------------------------------------------

    elif command == "/HELP":

        await send_report(
            chat_id,
            """
📋 MAFIA EMPIRE AUTO MANAGER

━━━━━━━━━━━━━━━━━━
🏭 FACTORY MANAGER
━━━━━━━━━━━━━━━━━━

/GO - شروع مدیریت خودکار
/OFF - توقف مدیریت خودکار
/TEST - اجرای تست کامل
/INFO - مشاهده وضعیت

/P نام محصول - تعیین محصول
/MIN عدد - تعیین حداقل قیمت
/CAP عدد - تعیین ظرفیت انبار
/TIME عدد - تعیین فاصله بررسی (دقیقه)
/PREDICT - مشاهده پیش‌بینی قیمت یک ساعت آینده

━━━━━━━━━━━━━━━━━━
⚔️ AUTO BATTLE
━━━━━━━━━━━━━━━━━━

/W کلمه

تعیین کلمه عملیات.
نیازی به Reply نیست.

مثال:
/W سلام

/N

شروع Auto Battle برای شخص موردنظر.

نحوه استفاده:
1. پیام شخص موردنظر را پیدا کن.
2. روی پیام او Reply کن.
3. دستور /N را ارسال کن.

هر شخص عملیات مستقل خودش را دارد.

/NX

توقف Auto Battle شخص موردنظر.

نحوه استفاده:
1. روی پیام همان شخص Reply کن.
2. دستور /NX را ارسال کن.

با این دستور فقط عملیات همان شخص متوقف می‌شود.
عملیات اشخاص دیگر ادامه پیدا می‌کند.

━━━━━━━━━━━━━━━━━━
📨 REPORTS
━━━━━━━━━━━━━━━━━━

تمام گزارش‌ها، خطاها، نتایج عملیات
و پیام‌های توقف در Saved Messages ارسال می‌شوند.

تنظیمات کارخانه برای هر گروه جداگانه ذخیره می‌شوند.
"""
        )


# =========================================================
# 12. AUTO BATTLE SYSTEM
# =========================================================

battle_settings = _AccountDict("battle_settings")
battle_tasks = _AccountDict("battle_tasks")


# ---------------------------------------------------------
# SETTINGS
# ---------------------------------------------------------

def load_battle_settings():

    account = CUR.get()

    if account.battle_settings_loaded:
        return

    account.battle_settings_loaded = True

    battle_file = acc_file("mafia_battle_settings.json")

    if not battle_file.exists():
        account.battle_settings = {}
        return

    try:

        data = json.loads(
            battle_file.read_text(
                encoding="utf-8"
            )
        )

        account.battle_settings = (
            data if isinstance(data, dict) else {}
        )

    except Exception as error:

        print("[BATTLE SETTINGS ERROR]", repr(error))
        account.battle_settings = {}


def save_battle_settings():

    acc_file("mafia_battle_settings.json").write_text(
        json.dumps(
            battle_settings._d(),
            ensure_ascii=False,
            indent=4
        ),
        encoding="utf-8"
    )


def get_battle_word(chat_id):

    load_battle_settings()

    return battle_settings.get(str(chat_id), {}).get(
        "word", ""
    )


def set_battle_word(chat_id, word):

    load_battle_settings()

    key = str(chat_id)

    if key not in battle_settings:
        battle_settings[key] = {}

    battle_settings[key]["word"] = word

    save_battle_settings()


# ---------------------------------------------------------
# FIND LAST ACCOUNT MESSAGE
# ---------------------------------------------------------

async def get_last_account_message(
    chat_id,
    target_user_id,
    before_id=None
):

    kwargs = {}

    if before_id is not None:
        kwargs["max_id"] = before_id

    async for message in client.iter_messages(
        chat_id,
        limit=100,
        from_user=target_user_id,
        **kwargs
    ):

        text = (message.raw_text or "").strip()

        if re.match(
            r"^/(?:N|NX|W|NSTOP|WORD)\b",
            text,
            re.IGNORECASE
        ):
            continue

        return message

    return None


# ---------------------------------------------------------
# EXTRACT WIN PERCENTAGE
# ---------------------------------------------------------

def get_battle_percentage(text):

    text = normalize_market_line(text or "")

    text = normalize_digits(text)

    match = re.search(
        r"برآورد\s*برد\s*[:：]\s*(\d{1,3})\s*(?:٪|%)?",
        text
    )

    if not match:
        return None

    percentage = int(match.group(1))

    if not 0 <= percentage <= 100:
        return None

    return percentage


# ---------------------------------------------------------
# WAIT FOR GAME REPLY
# ---------------------------------------------------------

async def wait_for_battle_reply(
    chat_id,
    sent_message_id,
    timeout=90
):

    loop = asyncio.get_running_loop()

    future = loop.create_future()

    async def check_reply(event):

        if future.done():
            return

        message = event.message

        if message.chat_id != chat_id:
            return

        if message.reply_to_msg_id != sent_message_id:
            return

        text = message.raw_text or ""

        percentage = get_battle_percentage(text)

        if percentage is None:
            return

        if not message.buttons:
            return

        print("[BATTLE] Game reply detected")
        print("[BATTLE] Percentage:", percentage)

        future.set_result(message)

    new_builder = events.NewMessage(chats=chat_id)

    edit_builder = events.MessageEdited(chats=chat_id)

    client.add_event_handler(
        check_reply,
        new_builder
    )

    client.add_event_handler(
        check_reply,
        edit_builder
    )

    try:

        result = await asyncio.wait_for(
            future,
            timeout=timeout
        )

        return result

    finally:

        client.remove_event_handler(
            check_reply,
            new_builder
        )

        client.remove_event_handler(
            check_reply,
            edit_builder
        )


# ---------------------------------------------------------
# CLICK LEFT / RIGHT BUTTON
# ---------------------------------------------------------

async def click_battle_button(message, side):

    buttons = message.buttons

    if not buttons or len(buttons[0]) < 2:

        raise RuntimeError(
            "دکمه چپ و راست به‌صورت مجزا پیدا نشد."
        )

    if side == "LEFT":

        row = 0
        column = 0

    elif side == "RIGHT":

        row = 0
        column = len(buttons[0]) - 1

    else:

        raise ValueError("Invalid button side")

    button = buttons[row][column]

    print(
        "[BATTLE] Clicking:",
        side,
        getattr(button, "text", "Unknown")
    )

    await message.click(row, column)


# ---------------------------------------------------------
# ONE BATTLE CYCLE
# ---------------------------------------------------------

async def run_battle_cycle(
    chat_id,
    target_user_id,
    before_id=None
):

    word = get_battle_word(chat_id)

    if not word:

        raise RuntimeError(
            "ابتدا با دستور /W کلمه را تعیین کن."
        )

    last_message = await get_last_account_message(
        chat_id,
        target_user_id=target_user_id,
        before_id=before_id
    )

    if last_message is None:

        raise RuntimeError(
            "پیام قبلی اکانت فیک پیدا نشد."
        )

    print(
        "[BATTLE] Replying to message:",
        last_message.id
    )

    sent_message = await client.send_message(
        chat_id,
        word,
        reply_to=last_message.id
    )

    print("[BATTLE] Word sent:", word)

    game_message = await wait_for_battle_reply(
        chat_id,
        sent_message.id,
        timeout=90
    )

    percentage = get_battle_percentage(
        game_message.raw_text or ""
    )

    if percentage is None:

        raise RuntimeError(
            "درصد برد قابل تشخیص نیست."
        )

    print(
        "[BATTLE] Win percentage:",
        percentage
    )

    if percentage >= 50:

        print("[BATTLE] Result: 50% OR HIGHER")

        await click_battle_button(
            game_message,
            "LEFT"
        )

        return True

    else:

        print("[BATTLE] Result: BELOW 50%")

        await click_battle_button(
            game_message,
            "RIGHT"
        )

        return False


# ---------------------------------------------------------
# AUTOMATIC BATTLE LOOP
# ---------------------------------------------------------

async def automatic_battle_loop(
    chat_id,
    command_message_id,
    target_user_id
):

    print("\n[BATTLE] AUTOMATIC SYSTEM STARTED")
    print("[BATTLE] TARGET:", target_user_id)

    first_cycle = True

    try:

        while True:

            if first_cycle:

                before_id = command_message_id

            else:

                before_id = None

            first_cycle = False

            result = await run_battle_cycle(
                chat_id,
                target_user_id=target_user_id,
                before_id=before_id
            )

            if not result:

                await send_report(
                    chat_id,
                    f"🛑 عملیات متوقف شد.\n"
                    f"شخص هدف: `{target_user_id}`\n"
                    f"درصد برد کمتر از ۵۰٪ بود.\n"
                    f"برای شروع مجدد روی پیام شخص Reply کن و /N را اجرا کن."
                )

                print("[BATTLE] AUTOMATIC SYSTEM STOPPED")

                return

            delay = random.randint(360, 600)

            print(
                f"[BATTLE] Next cycle in "
                f"{delay // 60} minutes"
            )

            await asyncio.sleep(delay)

    except asyncio.CancelledError:

        print("[BATTLE] STOP REQUESTED")
        raise

    except Exception as error:

        print(
            "[BATTLE ERROR]",
            repr(error)
        )

        await send_report(
            chat_id,
            f"⚠️ عملیات متوقف شد.\n"
            f"شخص هدف: `{target_user_id}`\n"
            f"خطا: {error}\n"
            f"برای شروع مجدد /N را اجرا کن."
        )


# ---------------------------------------------------------
# BATTLE COMMAND HANDLER
# ---------------------------------------------------------

BATTLE_PATTERN = r"(?i)^/(?:NX|N|W)(?:\s+.*)?$"


async def battle_command_handler(event):

    if not event.is_group:
        return

    raw_text = (event.raw_text or "").strip()

    parts = raw_text.split(maxsplit=1)

    command = parts[0].upper()

    argument = (
        parts[1].strip()
        if len(parts) > 1
        else ""
    )

    chat_id = event.chat_id

    # -----------------------------------------------------
    # W - CHANGE WORD
    # -----------------------------------------------------

    if command == "/W":

        if not argument:

            await send_report(
                chat_id,
                "فرمت صحیح:\n/W کلمه"
            )

            return

        set_battle_word(
            chat_id,
            argument
        )

        await send_report(
            chat_id,
            f"✅ کلمه عملیات تنظیم شد:\n{argument}"
        )

        return

    # -----------------------------------------------------
    # N / NX REQUIRE REPLY
    # -----------------------------------------------------

    reply_message = await event.get_reply_message()

    if not reply_message:

        await send_report(
            chat_id,
            "⚠️ این دستور باید روی پیام شخص موردنظر Reply شود."
        )

        return

    target_user_id = reply_message.sender_id

    if not target_user_id:

        await send_report(
            chat_id,
            "شناسه فرستنده پیام قابل تشخیص نیست."
        )

        return

    # کلید مستقل برای هر شخص در هر گروه
    task_key = (chat_id, target_user_id)

    # -----------------------------------------------------
    # N - START
    # -----------------------------------------------------

    if command == "/N":

        task = battle_tasks.get(task_key)

        if task and not task.done():

            await send_report(
                chat_id,
                f"⚠️ عملیات Auto Battle برای این شخص از قبل فعال است.\n"
                f"شناسه: `{target_user_id}`"
            )

            return

        word = get_battle_word(chat_id)

        if not word:

            await send_report(
                chat_id,
                "⚠️ ابتدا کلمه را تعیین کن:\n"
                "/W کلمه"
            )

            return

        battle_tasks[task_key] = asyncio.create_task(
            automatic_battle_loop(
                chat_id,
                event.id,
                target_user_id
            )
        )

        await send_report(
            chat_id,
            "🚀 سیستم Auto Battle فعال شد.\n"
            f"شناسه شخص هدف: `{target_user_id}`\n"
            f"کلمه: {word}\n"
            "در انتظار پاسخ Mafia Empire..."
        )

    # -----------------------------------------------------
    # NX - STOP
    # -----------------------------------------------------

    elif command == "/NX":

        task = battle_tasks.get(task_key)

        if task and not task.done():

            task.cancel()

            await send_report(
                chat_id,
                "⏹ درخواست توقف Auto Battle ارسال شد.\n"
                f"شخص هدف: `{target_user_id}`"
            )

        else:

            await send_report(
                chat_id,
                "سیستم Auto Battle برای این شخص فعال نیست.\n"
                f"شناسه: `{target_user_id}`"
            )


# =========================================================
# 13. MULTI-ACCOUNT MANAGER
# =========================================================
# هر اکانت: Session، تنظیمات، تسک‌ها و تاریخچه‌ی قیمت جداگانه دارد.
# ادمین فقط «سلف» هر اکانت را مدیریت می‌کند (روشن/خاموش/مدت)؛
# به پیام‌ها و چت‌های اکانت‌ها هیچ دسترسی‌ای ندارد.

def bind(account, handler):

    async def wrapper(event):

        token = CUR.set(account)

        try:

            # سلف خاموش یا مدت تمام‌شده: هیچ دستوری اجرا نمی‌شود
            if not account.is_active():
                return

            await handler(event)

        finally:

            CUR.reset(token)

    return wrapper


def attach_handlers(account):

    if account.handlers_attached:
        return

    account.client.add_event_handler(
        bind(account, command_handler),
        events.NewMessage(outgoing=True, pattern=COMMAND_PATTERN)
    )

    account.client.add_event_handler(
        bind(account, battle_command_handler),
        events.NewMessage(outgoing=True, pattern=BATTLE_PATTERN)
    )

    account.handlers_attached = True


def load_accounts_meta():

    global accounts_meta

    try:

        if ACCOUNTS_FILE.exists():

            data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
            accounts_meta = data if isinstance(data, dict) else {}

        else:

            accounts_meta = {}

    except Exception as error:

        print("[ACCOUNTS LOAD ERROR]", repr(error))
        accounts_meta = {}


def save_accounts_meta():

    temporary = ACCOUNTS_FILE.with_suffix(".tmp")

    temporary.write_text(
        json.dumps(accounts_meta, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    temporary.replace(ACCOUNTS_FILE)


def migrate_legacy():

    """اگر قبلاً با نسخه‌ی تک‌اکانتی وارد شده‌ای، همان اکانت به‌عنوان a1 منتقل می‌شود."""

    old_session = DATA_DIR / "mafia_auto_manager.session"

    if accounts_meta or not old_session.exists():
        return

    folder = ACCOUNTS_DIR / "a1"
    folder.mkdir(parents=True, exist_ok=True)

    moves = [
        ("mafia_auto_manager.session", "session.session"),
        ("mafia_settings.json", "mafia_settings.json"),
        ("mafia_price_history.json", "mafia_price_history.json"),
        ("mafia_price_learning_state.json", "mafia_price_learning_state.json"),
        ("mafia_battle_settings.json", "mafia_battle_settings.json"),
    ]

    for old_name, new_name in moves:

        source = DATA_DIR / old_name

        if source.exists():
            source.replace(folder / new_name)

    accounts_meta["a1"] = {
        "name": "اکانت اصلی",
        "user_id": None,
        "enabled": True,
        "expires_at": None,
        "created": int(time.time())
    }

    save_accounts_meta()

    print("[MIGRATE] Legacy session moved to account a1")


def new_account_id():

    numbers = [
        int(key[1:])
        for key in accounts_meta
        if key[1:].isdigit()
    ]

    return f"a{max(numbers, default=0) + 1}"


def load_account_state(account):

    token = CUR.set(account)

    try:

        load_settings()
        load_price_history()
        load_battle_settings()

    finally:

        CUR.reset(token)


async def start_account(account_id, account_client=None):

    account = Account(account_id, account_client)
    accounts[account_id] = account

    load_account_state(account)

    if not account.client.is_connected():
        await account.client.connect()

    account.authorized = await account.client.is_user_authorized()

    if account.authorized:
        attach_handlers(account)

    return account


def remaining_text(meta):

    expires_at = meta.get("expires_at")

    if not expires_at:
        return "نامحدود"

    remaining = expires_at - time.time()

    if remaining <= 0:
        return "منقضی"

    return f"{math.ceil(remaining / 86400)} روز"


def status_icon(account_id):

    account = accounts.get(account_id)

    if account is None:
        return "⚪"

    if not account.authorized:
        return "⚠️"

    return "🟢" if account.is_active() else "🔴"


def disable_account(account_id):

    meta = accounts_meta.get(account_id)

    if meta is None:
        return

    meta["enabled"] = False
    save_accounts_meta()

    account = accounts.get(account_id)

    if account is not None:
        account.stop_tasks()


async def expiry_watcher():

    while True:

        await asyncio.sleep(60)

        try:

            now = time.time()

            for account_id, meta in list(accounts_meta.items()):

                expires_at = meta.get("expires_at")

                if meta.get("enabled") and expires_at and now >= expires_at:

                    disable_account(account_id)

                    await bot.send_message(
                        OWNER_ID,
                        f"⏰ مدت اکانت «{meta.get('name', account_id)}» تمام شد.\n"
                        "سلف این اکانت خاموش شد. برای روشن شدن، مدت را تمدید کن: /panel"
                    )

        except Exception as error:

            print("[EXPIRY WATCHER ERROR]", repr(error))


# =========================================================
# 14. ADMIN PANEL (داخل همین ربات)
# =========================================================

flow = {"step": None}


def only_digits(text):

    return re.sub(r"\D", "", normalize_digits(text or ""))


async def safe_delete(event):

    try:
        await event.delete()
    except Exception:
        pass


async def show(event, text, buttons):

    try:
        await event.edit(text, buttons=buttons)
    except Exception:
        await event.respond(text, buttons=buttons)


async def reset_flow():

    temporary = flow.get("client")
    account_id = flow.get("account_id")

    if temporary is not None:

        try:
            await temporary.disconnect()
        except Exception:
            pass

        if account_id and account_id not in accounts_meta:
            shutil.rmtree(ACCOUNTS_DIR / account_id, ignore_errors=True)

    flow.clear()
    flow["step"] = None


def main_panel():

    lines = [
        "🎛 پنل مدیریت اکانت‌ها",
        "━━━━━━━━━━━━━━━━━━"
    ]

    if not accounts_meta:

        lines.append("هنوز اکانتی اضافه نشده است.")

    else:

        lines.append(f"تعداد اکانت‌ها: {len(accounts_meta)}")
        lines.append("🟢 فعال   🔴 خاموش   ⚠️ نیاز به ورود مجدد")

    rows = []

    for account_id, meta in accounts_meta.items():

        rows.append([
            Button.inline(
                f"{status_icon(account_id)} {meta.get('name', '?')} • {remaining_text(meta)}",
                data=f"acc:{account_id}".encode()
            )
        ])

    rows.append([Button.inline("➕ افزودن اکانت", data=b"add")])

    return "\n".join(lines), rows


def account_panel(account_id):

    meta = accounts_meta.get(account_id)

    if meta is None:
        return main_panel()

    account = accounts.get(account_id)

    running = 0

    if account is not None:

        running = sum(
            1
            for group in (account.tasks, account.battle_tasks)
            for task in group.values()
            if not task.done()
        )

    if account is not None and not account.authorized:
        status = "⚠️ نیاز به ورود مجدد (حذف کن و دوباره اضافه کن)"
    elif account is not None and account.is_active():
        status = "🟢 فعال"
    elif meta.get("enabled"):
        status = "⏰ منقضی شده"
    else:
        status = "🔴 خاموش"

    expires_at = meta.get("expires_at")

    end_text = (
        time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(expires_at))
        if expires_at
        else "—"
    )

    text = (
        f"👤 {meta.get('name', '?')}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"شناسه داخلی: {account_id}\n"
        f"وضعیت سلف: {status}\n"
        f"مدت باقی‌مانده: {remaining_text(meta)}\n"
        f"پایان مدت: {end_text}\n"
        f"عملیات در حال اجرا: {running}"
    )

    toggle_label = (
        "🔴 خاموش کردن سلف"
        if meta.get("enabled")
        else "🟢 روشن کردن سلف"
    )

    rows = [
        [Button.inline(toggle_label, data=f"tog:{account_id}".encode())],
        [
            Button.inline("⏳ تنظیم مدت", data=f"days:{account_id}".encode()),
            Button.inline("➕ تمدید", data=f"ext:{account_id}".encode())
        ],
        [Button.inline("🗑 حذف اکانت", data=f"del:{account_id}".encode())],
        [Button.inline("⬅️ بازگشت", data=b"list")]
    ]

    return text, rows


async def begin_add(event):

    await reset_flow()

    flow.update(step="phone", account_id=new_account_id())

    await event.respond(
        "📱 شماره اکانت را با کد کشور بفرست.\n"
        "مثال: +989121234567\n\n"
        "برای لغو: /cancel"
    )


async def finish_add(event):

    temporary = flow["client"]
    account_id = flow["account_id"]

    await safe_delete(event)  # پاک کردن پیام حاوی کد/رمز

    me = await temporary.get_me()

    for meta in accounts_meta.values():

        if meta.get("user_id") and meta.get("user_id") == me.id:

            try:
                await temporary.log_out()
            except Exception:
                pass

            shutil.rmtree(ACCOUNTS_DIR / account_id, ignore_errors=True)

            flow.clear()
            flow["step"] = None

            await event.respond("⚠️ این اکانت قبلاً اضافه شده است.")

            return

    accounts_meta[account_id] = {
        "name": me.first_name or me.username or account_id,
        "user_id": me.id,
        "enabled": True,
        "expires_at": None,
        "created": int(time.time())
    }

    save_accounts_meta()

    await start_account(account_id, temporary)

    flow.clear()
    flow.update(step="days", account_id=account_id, mode="set")

    await event.respond(
        f"✅ اکانت «{accounts_meta[account_id]['name']}» وصل شد.\n\n"
        "⏳ چند روز سلف این اکانت فعال باشد؟\n"
        "یک عدد بفرست (۰ = نامحدود)."
    )


@bot.on(events.CallbackQuery)
async def panel_callback(event):

    if event.sender_id != OWNER_ID:
        await event.answer("⛔ دسترسی نداری.", alert=True)
        return

    data = (event.data or b"").decode()
    action, _, account_id = data.partition(":")

    if action == "list":

        await reset_flow()

        text, buttons = main_panel()
        await show(event, text, buttons)
        await event.answer()
        return

    if action == "add":

        await event.answer()
        await begin_add(event)
        return

    if account_id not in accounts_meta:

        await event.answer("این اکانت دیگر وجود ندارد.", alert=True)

        text, buttons = main_panel()
        await show(event, text, buttons)
        return

    if action == "acc":

        await reset_flow()

        text, buttons = account_panel(account_id)
        await show(event, text, buttons)
        await event.answer()

    elif action == "tog":

        meta = accounts_meta[account_id]

        if meta.get("enabled"):

            disable_account(account_id)
            await event.answer("سلف خاموش شد.")

        else:

            expires_at = meta.get("expires_at")

            if expires_at and time.time() >= expires_at:

                await event.answer(
                    "مدت این اکانت تمام شده؛ ابتدا مدت را تنظیم یا تمدید کن.",
                    alert=True
                )
                return

            meta["enabled"] = True
            save_accounts_meta()

            await event.answer("سلف روشن شد.")

        text, buttons = account_panel(account_id)
        await show(event, text, buttons)

    elif action in ("days", "ext"):

        await reset_flow()

        flow.update(
            step="days",
            account_id=account_id,
            mode="set" if action == "days" else "ext"
        )

        await event.answer()

        if action == "days":

            await event.respond(
                "⏳ از همین الان چند روز فعال باشد؟\n"
                "یک عدد بفرست (۰ = نامحدود).\n"
                "برای لغو: /cancel"
            )

        else:

            await event.respond(
                "➕ چند روز به مدت فعلی اضافه شود؟\n"
                "یک عدد بزرگ‌تر از صفر بفرست.\n"
                "برای لغو: /cancel"
            )

    elif action == "del":

        name = accounts_meta[account_id].get("name", account_id)

        await show(
            event,
            f"🗑 اکانت «{name}» حذف شود؟\n"
            "سلف خاموش و Session این اکانت از تلگرام خارج می‌شود "
            "و همه‌ی تنظیماتش پاک می‌شود.",
            [
                [Button.inline("✅ بله، حذف کن", data=f"delok:{account_id}".encode())],
                [Button.inline("⬅️ نه", data=f"acc:{account_id}".encode())]
            ]
        )

        await event.answer()

    elif action == "delok":

        account = accounts.pop(account_id, None)

        if account is not None:

            account.stop_tasks()

            try:
                if await account.client.is_user_authorized():
                    await account.client.log_out()
            except Exception as error:
                print("[DELETE LOGOUT ERROR]", repr(error))

            try:
                await account.client.disconnect()
            except Exception:
                pass

        accounts_meta.pop(account_id, None)
        save_accounts_meta()

        shutil.rmtree(ACCOUNTS_DIR / account_id, ignore_errors=True)

        await event.answer("اکانت حذف شد.")

        text, buttons = main_panel()
        await show(event, text, buttons)


@bot.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
async def bot_message_handler(event):

    if event.sender_id != OWNER_ID:
        return

    text = (event.raw_text or "").strip()
    step = flow.get("step")

    # ---------- commands ----------

    if text.lower() in ("/start", "/panel", "/menu"):

        await reset_flow()

        panel_text, buttons = main_panel()
        await event.respond(panel_text, buttons=buttons)
        return

    if text.lower() in ("/login", "/add"):

        await begin_add(event)
        return

    if text.lower() == "/cancel":

        await reset_flow()
        await event.respond("لغو شد. برای پنل: /panel")
        return

    # ---------- phone ----------

    if step == "phone":

        phone = "+" + only_digits(text)
        account_id = flow["account_id"]

        folder = ACCOUNTS_DIR / account_id
        folder.mkdir(parents=True, exist_ok=True)

        temporary = TelegramClient(
            str(folder / "session"),
            API_ID,
            API_HASH
        )

        try:

            await temporary.connect()
            sent = await temporary.send_code_request(phone)

        except PhoneNumberInvalidError:

            await temporary.disconnect()
            await event.respond("❌ شماره نامعتبر است. دوباره بفرست.")
            return

        except FloodWaitError as error:

            await temporary.disconnect()
            await event.respond(
                f"⏳ تلگرام محدودیت گذاشته. {error.seconds} ثانیه صبر کن."
            )
            return

        except Exception as error:

            await temporary.disconnect()
            await event.respond(f"❌ خطا: {error}")
            return

        flow.update(
            step="code",
            phone=phone,
            hash=sent.phone_code_hash,
            client=temporary
        )

        await event.respond(
            "📨 کد به تلگرام اکانت ارسال شد.\n\n"
            "⚠️ کد را با خط تیره یا فاصله بفرست، مثلاً: 1-2-3-4-5\n"
            "(اگر کد را پشت‌سرهم بفرستی تلگرام آن را باطل می‌کند)"
        )
        return

    # ---------- code ----------

    if step == "code":

        code = only_digits(text)
        temporary = flow["client"]

        try:

            await temporary.sign_in(
                phone=flow["phone"],
                code=code,
                phone_code_hash=flow["hash"]
            )

        except SessionPasswordNeededError:

            flow["step"] = "password"

            await safe_delete(event)
            await event.respond("🔐 رمز تأیید دومرحله‌ای را بفرست.")
            return

        except PhoneCodeInvalidError:

            await event.respond("❌ کد اشتباه است. دوباره بفرست.")
            return

        except PhoneCodeExpiredError:

            await reset_flow()
            await event.respond("⌛ کد منقضی شد. دوباره /login بزن.")
            return

        except Exception as error:

            await event.respond(f"❌ خطا: {error}")
            return

        await finish_add(event)
        return

    # ---------- 2FA password ----------

    if step == "password":

        temporary = flow["client"]

        try:

            await temporary.sign_in(password=text)

        except PasswordHashInvalidError:

            await event.respond("❌ رمز اشتباه است. دوباره بفرست.")
            return

        except Exception as error:

            await event.respond(f"❌ خطا: {error}")
            return

        await finish_add(event)
        return

    # ---------- days ----------

    if step == "days":

        account_id = flow.get("account_id")
        mode = flow.get("mode", "set")

        if account_id not in accounts_meta:

            await reset_flow()
            await event.respond("این اکانت دیگر وجود ندارد. /panel")
            return

        try:

            value = clean_number(text)

        except ValueError:

            await event.respond("❌ فقط یک عدد بفرست.")
            return

        if mode == "ext" and value <= 0:

            await event.respond("❌ برای تمدید، عدد باید بزرگ‌تر از صفر باشد.")
            return

        meta = accounts_meta[account_id]
        now = time.time()

        if mode == "ext" and meta.get("expires_at") and value > 0:

            base = max(now, meta["expires_at"])
            meta["expires_at"] = int(base + value * 86400)

        elif value > 0:

            meta["expires_at"] = int(now + value * 86400)

        else:

            meta["expires_at"] = None

        meta["enabled"] = True
        save_accounts_meta()

        await reset_flow()

        panel_text, buttons = account_panel(account_id)
        await event.respond(panel_text, buttons=buttons)
        return

    await event.respond("برای باز کردن پنل: /panel")


# =========================================================
# 15. MAIN
# =========================================================

async def main():

    print("=" * 50)
    print("MAFIA EMPIRE AUTO MANAGER (MULTI-ACCOUNT)")
    print("=" * 50)

    await bot.start(bot_token=BOT_TOKEN)

    load_accounts_meta()
    migrate_legacy()

    broken = []

    for account_id in list(accounts_meta):

        try:

            account = await start_account(account_id)

            print(
                f"[ACCOUNT] {account_id} "
                f"{accounts_meta[account_id].get('name')} "
                f"authorized={account.authorized}"
            )

            if not account.authorized:
                broken.append(accounts_meta[account_id].get("name", account_id))

        except Exception as error:

            print("[ACCOUNT START ERROR]", account_id, repr(error))
            broken.append(accounts_meta[account_id].get("name", account_id))

    watcher = asyncio.create_task(expiry_watcher())

    message = (
        f"✅ سرویس بالا آمد. تعداد اکانت‌ها: {len(accounts_meta)}\n"
        "برای مدیریت: /panel"
    )

    if broken:
        message += "\n\n⚠️ نیاز به ورود مجدد: " + "، ".join(broken)

    try:

        await bot.send_message(OWNER_ID, message)

    except Exception as error:

        print("[BOT NOTIFY ERROR]", repr(error))

    print("=" * 50)
    print("Waiting for commands...")
    print("=" * 50)

    try:

        await bot.run_until_disconnected()

    finally:

        watcher.cancel()


if __name__ == "__main__":

    try:

        asyncio.run(main())

    except KeyboardInterrupt:

        print("\nProgram stopped.")

    except Exception as error:

        print("\nFATAL ERROR:", repr(error))
