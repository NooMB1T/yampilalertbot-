import asyncio
import aiohttp
import logging
from datetime import datetime, timezone, timedelta
import os
import json
import re

from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)
from telegram.error import TelegramError, NetworkError, RetryAfter
from aiohttp import web

# ════════════════════════════════════════════
#              КОНФІГУРАЦІЯ
# ════════════════════════════════════════════
BOT_TOKEN         = os.getenv("BOT_TOKEN",     "8693341837:AAF2lK6bGR3uoLz1kfkZt8IjDQIF18YXHN8")
CHANNEL_ID        = os.getenv("CHANNEL_ID",    "@yampilnews")
ALERT_API_KEY     = os.getenv("ALERT_API_KEY", "b3de42c9:736017aa6745a605c155108e221d31a8")
ADMIN_IDS_RAW     = os.getenv("ADMIN_IDS", "")
ADMIN_IDS         = set(int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip().isdigit())

RENDER_URL        = os.getenv("RENDER_URL", "https://mapyampilalert.onrender.com")
PORT              = int(os.getenv("PORT", 10000))
WEBHOOK_PATH      = "/webhook"
WEBHOOK_URL       = f"{RENDER_URL}{WEBHOOK_PATH}"

ALERT_API_URL     = "https://api.ukrainealarm.com/api/v3/alerts"
TARGET_REGION     = "Хмельницька"
CHECK_INTERVAL    = 30
KYIV_TZ           = timezone(timedelta(hours=3))

MAP_URL           = "https://alerts.in.ua/"
MAP_IMAGE_URL     = "https://alerts.in.ua/map.png"

REGISTERED_FILE   = "/tmp/users.json"
BANNED_FILE       = "/tmp/banned.json"
QUESTIONS_FILE    = "/tmp/questions.json"
REGIONS_FILE      = "/tmp/regions.json"

DEFAULT_REGION_ID   = "31004"
DEFAULT_REGION_NAME = "Шепетівський р-н, Хмельницька обл."

REGIONS = {
    "31004": "Шепетівський р-н (Хмельницька)",
    "21":    "Хмельницька область",
    "19":    "Харківська область",
    "4":     "Донецька область",
    "17":    "Сумська область",
    "24":    "Чернігівська область",
    "9":     "Київська область",
    "25":    "м. Київ",
    "3":     "Дніпропетровська область",
    "15":    "Полтавська область",
    "7":     "Запорізька область",
    "20":    "Херсонська область",
    "13":    "Миколаївська область",
    "14":    "Одеська область",
    "1":     "Вінницька область",
    "5":     "Житомирська область",
    "2":     "Волинська область",
    "16":    "Рівненська область",
    "12":    "Львівська область",
    "8":     "Івано-Франківська область",
    "18":    "Тернопільська область",
    "23":    "Чернівецька область",
    "6":     "Закарпатська область",
    "22":    "Черкаська область",
    "10":    "Кіровоградська область",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)s │ %(message)s"
)
log = logging.getLogger(__name__)

alert_active: bool | None = None
stats = {
    "alerts": 0,
    "allclear": 0,
    "messages": 0,
    "start": None
}

# Conversation states
REG_NAME, REG_PHONE, REG_ADDRESS = 1, 2, 3

# ════════════════════════════════════════════
#           ЗБЕРІГАННЯ ДАНИХ
# ════════════════════════════════════════════
def _load(path: str, default):
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception as e:
        log.warning("Помилка читання %s: %s", path, e)
    return default

def _save(path: str, data) -> None:
    try:
        with open(path, "w") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        log.warning("Помилка запису %s: %s", path, e)

def load_set(path): return set(_load(path, []))
def save_set(path, s): _save(path, list(s))

registered = load_set(REGISTERED_FILE)
banned     = load_set(BANNED_FILE)

def is_reg(uid):   return uid in registered
def is_ban(uid):   return uid in banned
def reg_user(uid): registered.add(uid); save_set(REGISTERED_FILE, registered)
def ban_user(uid): banned.add(uid);     save_set(BANNED_FILE, banned)
def unban_user(uid): banned.discard(uid); save_set(BANNED_FILE, banned)

def get_questions(): return _load(QUESTIONS_FILE, [])
def add_question(q): qs = get_questions(); qs.append(q); _save(QUESTIONS_FILE, qs)

def load_user_regions(): return _load(REGIONS_FILE, {})
def save_user_regions(r): _save(REGIONS_FILE, r)

def get_user_region(uid: int) -> tuple[str, str]:
    """Повертає (region_id, region_name) для користувача"""
    regions = load_user_regions()
    rid = regions.get(str(uid), DEFAULT_REGION_ID)
    return rid, REGIONS.get(rid, DEFAULT_REGION_NAME)

def set_user_region(uid: int, region_id: str) -> None:
    regions = load_user_regions()
    regions[str(uid)] = region_id
    save_user_regions(regions)

# ════════════════════════════════════════════
#              ДОПОМІЖНІ ФУНКЦІЇ
# ════════════════════════════════════════════
def now_kyiv(): return datetime.now(KYIV_TZ)
def now_str():  return now_kyiv().strftime("%H:%M  %d.%m.%Y")

def uptime():
    if not stats["start"]: return "—"
    d = now_kyiv() - stats["start"]
    h, r = divmod(int(d.total_seconds()), 3600)
    return f"{h}г {r//60}хв"

def hello():
    h = now_kyiv().hour
    if  5 <= h < 12: return "🌤 Доброго ранку"
    if 12 <= h < 17: return "☀️ Доброго дня"
    if 17 <= h < 22: return "🌆 Доброго вечора"
    return "🌙 Доброї ночі"

def fname(update):
    u = update.effective_user
    return u.first_name if u and u.first_name else "друже"

def alert_text():
    if alert_active is None: return "⏳ Перевіряємо..."
    return "🔴 АКТИВНА" if alert_active else "🟢 Спокійно"

def is_admin(update): return update.effective_user.id in ADMIN_IDS

def check_phone(p):
    return len(re.sub(r'\D', '', p)) >= 10

# ════════════════════════════════════════════
#               КЛАВІАТУРИ
# ════════════════════════════════════════════
def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚨 Тривога",      callback_data="status"),
         InlineKeyboardButton("🗺 Карта",         callback_data="map")],
        [InlineKeyboardButton("🏠 Укриття",      callback_data="shelters"),
         InlineKeyboardButton("📞 Екстренні",    callback_data="emergency")],
        [InlineKeyboardButton("📋 Правила",      callback_data="rules"),
         InlineKeyboardButton("📍 Контакти",     callback_data="contacts")],
        [InlineKeyboardButton("🌍 Мій регіон",   callback_data="region"),
         InlineKeyboardButton("❓ Запитання",    callback_data="ask")],
        [InlineKeyboardButton("ℹ️ Про бота",     callback_data="about")],
        [InlineKeyboardButton("📢 Канал",        url="https://t.me/yampilnews")],
    ])

def kb_regions(page: int = 0):
    """Клавіатура вибору регіону з пагінацією"""
    items = list(REGIONS.items())
    per_page = 8
    start = page * per_page
    end   = start + per_page
    chunk = items[start:end]
    
    rows = []
    for i in range(0, len(chunk), 2):
        row = []
        for rid, rname in chunk[i:i+2]:
            short = rname.replace(" область", "").replace(" обл.", "")
            row.append(InlineKeyboardButton(short, callback_data=f"setreg_{rid}"))
        rows.append(row)
    
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"regpage_{page-1}"))
    if end < len(items):
        nav.append(InlineKeyboardButton("▶️", callback_data=f"regpage_{page+1}"))
    if nav:
        rows.append(nav)
    
    rows.append([InlineKeyboardButton("🔙 Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)

def kb_status():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Оновити",  callback_data="status"),
        InlineKeyboardButton("🗺 Карта",    callback_data="map"),
        InlineKeyboardButton("🏠 Укриття", callback_data="shelters"),
    ]])

def kb_admin():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Оголошення в канал",  callback_data="a_post")],
        [InlineKeyboardButton("🔴 Тест тривога",        callback_data="a_talert"),
         InlineKeyboardButton("🟢 Тест відбій",         callback_data="a_tclear")],
        [InlineKeyboardButton("👥 Користувачі",         callback_data="a_users"),
         InlineKeyboardButton("❓ Питання",             callback_data="a_questions")],
        [InlineKeyboardButton("🚫 Заблокувати",         callback_data="a_ban"),
         InlineKeyboardButton("✅ Розблокувати",        callback_data="a_unban")],
    ])

# ════════════════════════════════════════════
#              UKRAINE ALARM API
# ════════════════════════════════════════════
async def fetch_alarm(session: aiohttp.ClientSession) -> bool:
    try:
        headers = {
            "Authorization": ALERT_API_KEY,
        }
        async with session.get(
            ALERT_API_URL,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status == 200:
                data = await r.json()
                # data — список регіонів, кожен має activeAlerts
                for region in data:
                    active = region.get("activeAlerts", [])
                    if active:
                        # Перевіряємо чи є тривога в Хмельницькій
                        reg_name = region.get("regionName", "")
                        if TARGET_REGION in reg_name:
                            for alert in active:
                                if alert.get("type") == "AIR":
                                    return True
                return False
            log.error("API відповів: %s", r.status)
            return alert_active or False
    except asyncio.TimeoutError:
        log.warning("API timeout")
        return alert_active or False
    except Exception as e:
        log.error("API error: %s", e)
        return alert_active or False

async def send_channel(bot: Bot, text: str, photo: str = None) -> None:
    try:
        if photo:
            await bot.send_photo(chat_id=CHANNEL_ID, photo=photo, caption=text)
        else:
            await bot.send_message(chat_id=CHANNEL_ID, text=text)
        stats["messages"] += 1
    except RetryAfter as e:
        log.warning("Flood wait %ss", e.retry_after)
        await asyncio.sleep(e.retry_after)
    except TelegramError as e:
        log.error("Telegram: %s", e)

# ════════════════════════════════════════════
#              ПЕРЕВІРКА ДОСТУПУ
# ════════════════════════════════════════════
async def gate(update) -> bool:
    uid = update.effective_user.id
    if is_ban(uid):
        await update.message.reply_text("🚫 Вас заблоковано.")
        return False
    if not is_reg(uid) and not is_admin(update):
        await update.message.reply_text(
            "👋 Привіт!\n\n"
            "Для доступу до бота потрібна реєстрація.\n\n"
            "▶️ /register"
        )
        return False
    return True

# ════════════════════════════════════════════
#              РЕЄСТРАЦІЯ
# ════════════════════════════════════════════
async def cmd_register(update, ctx):
    uid = update.effective_user.id
    if is_reg(uid):
        await update.message.reply_text("✅ Ти вже зареєстрований!\n\n▶️ /start")
        return ConversationHandler.END
    if is_ban(uid):
        await update.message.reply_text("🚫 Реєстрація недоступна.")
        return ConversationHandler.END
    await update.message.reply_text(
        "📋 РЕЄСТРАЦІЯ\n"
        "─────────────\n\n"
        "Крок 1 із 3\n\n"
        "👤 Як тебе звати?"
    )
    return REG_NAME

async def reg_name(update, ctx):
    ctx.user_data["name"] = update.message.text.strip()
    await update.message.reply_text(
        "📋 РЕЄСТРАЦІЯ\n"
        "─────────────\n\n"
        "Крок 2 із 3\n\n"
        "📱 Номер телефону?\n"
        "Приклад: +380 95 123 45 67"
    )
    return REG_PHONE

async def reg_phone(update, ctx):
    phone = update.message.text.strip()
    if not check_phone(phone):
        await update.message.reply_text(
            "❌ Невірний формат номера.\n\n"
            "Спробуй ще раз:"
        )
        return REG_PHONE
    ctx.user_data["phone"] = phone
    await update.message.reply_text(
        "📋 РЕЄСТРАЦІЯ\n"
        "─────────────\n\n"
        "Крок 3 із 3\n\n"
        "🏠 Твоя адреса?\n"
        "Приклад: вул. Центральна, 5"
    )
    return REG_ADDRESS

async def reg_address(update, ctx):
    ctx.user_data["address"] = update.message.text.strip()
    reg_user(update.effective_user.id)
    await update.message.reply_text(
        "✅ РЕЄСТРАЦІЮ ЗАВЕРШЕНО!\n"
        "─────────────────────\n\n"
        f"👤 Ім'я: {ctx.user_data['name']}\n"
        f"📱 Телефон: {ctx.user_data['phone']}\n"
        f"🏠 Адреса: {ctx.user_data['address']}\n\n"
        "▶️ /start"
    )
    return ConversationHandler.END

async def reg_cancel(update, ctx):
    await update.message.reply_text("❌ Реєстрацію скасовано.")
    return ConversationHandler.END

# ════════════════════════════════════════════
#              КОМАНДИ БОТА
# ════════════════════════════════════════════
async def cmd_start(update, ctx):
    if not await gate(update): return
    name = fname(update)
    st   = alert_text()
    
    msg = (
        f"{hello()}, {name}!\n"
        f"══════════════════════\n\n"
        f"📍 СМТ Ямпіль\n"
        f"Шепетівський р-н · Хмельницька обл.\n\n"
        f"🚨 Тривога зараз: {st}\n"
    )
    if alert_active:
        msg += "\n⚠️ НЕГАЙНО ДО УКРИТТЯ!\n🙏 Бережіть себе!\n"
    msg += "\n══════════════════════\nОберіть дію:"
    
    await update.message.reply_text(msg, reply_markup=kb_main())
    stats["messages"] += 1

async def cmd_status(update, ctx):
    if not await gate(update): return
    uid = update.effective_user.id
    rid, rname = get_user_region(uid)
    st = alert_text()
    msg = (
        f"🛡 СТАТУС ТРИВОГИ\n"
        f"══════════════════\n\n"
        f"📍 {rname}\n"
        f"🕐 {now_str()}\n\n"
        f"Стан: {st}\n"
    )
    if alert_active:
        msg += "\n⚠️ ТРИВОГА АКТИВНА!\n🏃 Прямуйте до укриття!\n🙏 Бережіть себе!"
    m = update.message or update.callback_query.message
    await m.reply_text(msg, reply_markup=kb_status())

async def cmd_region(update, ctx):
    if not await gate(update): return
    uid = update.effective_user.id
    rid, rname = get_user_region(uid)
    msg = (
        f"🌍 МІЙ РЕГІОН\n"
        f"══════════════\n\n"
        f"Зараз обрано:\n"
        f"📍 {rname}\n\n"
        f"Обери інший регіон:"
    )
    m = update.message or update.callback_query.message
    await m.reply_text(msg, reply_markup=kb_regions())

async def cmd_map(update, ctx):
    if not await gate(update): return
    m = update.message or (update.callback_query.message if update.callback_query else None)
    cap = (
        f"🗺 КАРТА ТРИВОГ УКРАЇНИ\n"
        f"══════════════════════\n\n"
        f"🕐 {now_str()}\n"
        f"📍 Хмельницька: {alert_text()}\n\n"
        f"🔴 є тривога  |  🟢 спокійно"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🌐 Онлайн-карта", url=MAP_URL),
        InlineKeyboardButton("🔄 Оновити",      callback_data="map"),
    ]])
    try:
        await m.reply_photo(photo=MAP_IMAGE_URL, caption=cap, reply_markup=kb)
    except Exception:
        await m.reply_text(f"{cap}\n\n🔗 {MAP_URL}", reply_markup=kb)

async def cmd_shelters(update, ctx):
    if not await gate(update): return
    msg = (
        "🏠 УКРИТТЯ В СМТ ЯМПІЛЬ\n"
        "════════════════════════\n\n"
        "① Підвал гімназії\n"
        "  📍 вул. Шкільна, 1\n\n"
        "② Будинок культури\n"
        "  📍 вул. Центральна\n\n"
        "③ Амбулаторія\n"
        "  📍 вул. Медична\n\n"
        "④ Адмін. будинок ОТГ\n"
        "  📍 вул. Незалежності\n\n"
        "═══════════════════════\n"
        "⚠️ При тривозі — до найближчого!\n"
        "📞 Деталі: 104"
    )
    m = update.message or update.callback_query.message
    await m.reply_text(msg)

async def cmd_emergency(update, ctx):
    if not await gate(update): return
    msg = (
        "📞 ЕКСТРЕНІ СЛУЖБИ\n"
        "════════════════════\n\n"
        "🚒  101 — Пожежна\n"
        "🚔  102 — Поліція\n"
        "🚑  103 — Швидка\n"
        "🛡  104 — ДСНС\n"
        "☎️  112 — Єдиний\n\n"
        "═══════════════════\n"
        "🇺🇦  1580 — МО України\n\n"
        "📻 Слідкуйте за офіційними каналами!"
    )
    m = update.message or update.callback_query.message
    await m.reply_text(msg)

async def cmd_rules(update, ctx):
    if not await gate(update): return
    msg = (
        "📋 ПРАВИЛА ПІД ЧАС ТРИВОГИ\n"
        "═══════════════════════════\n\n"
        "① Зупиніться та зберіть речі\n"
        "② Перейдіть до найближчого укриття\n"
        "③ Якщо немає — ляжте біля капітальної стіни\n"
        "④ Відійдіть від вікон і скла\n"
        "⑤ Вимкніть газ та прилади\n"
        "⑥ Телефон, документи, ліки — з собою\n"
        "⑦ Не виходьте до сигналу відбою\n"
        "⑧ Допоможіть сусідам\n\n"
        "═══════════════════════════\n"
        "💥 Після вибуху поряд:\n"
        "• Ляжте, прикрийте голову\n"
        "• Відійдіть від вікон\n"
        "• Чекайте рятувальників\n\n"
        "🙏 Бережіть себе!"
    )
    m = update.message or update.callback_query.message
    await m.reply_text(msg)

async def cmd_contacts(update, ctx):
    if not await gate(update): return
    msg = (
        "📍 КОНТАКТИ\n"
        "════════════\n\n"
        "🏛 Ямпільська ОТГ\n"
        "   🌐 yampil-gmada.gov.ua\n"
        "   📍 вул. Незалежності\n\n"
        "🚔 Поліція: 102\n"
        "🏥 Швидка: 103\n"
        "📢 Канал: @yampilnews\n\n"
        "ℹ️ Актуальні контакти\n"
        "уточнюйте в адміністрації"
    )
    m = update.message or update.callback_query.message
    await m.reply_text(msg)

async def cmd_ask_show(update, ctx):
    if not await gate(update): return
    m = update.callback_query.message
    await m.reply_text(
        "❓ ПИТАННЯ АДМІНІСТРАЦІЇ\n"
        "═══════════════════════\n\n"
        "Твоє питання отримає адміністратор і відповість тобі напряму.\n\n"
        "Введи команду:\n"
        "/ask_question <текст>\n\n"
        "Приклад:\n"
        "/ask_question Де найближче укриття?"
    )

async def cmd_ask(update, ctx):
    if not await gate(update): return
    text = " ".join(ctx.args).strip()
    if not text:
        await update.message.reply_text(
            "❓ Використання:\n/ask_question <твоє питання>"
        )
        return
    
    uid  = update.effective_user.id
    name = fname(update)
    
    add_question({"uid": uid, "name": name, "text": text, "time": now_str()})
    
    await update.message.reply_text(
        "✅ Питання надіслано!\n\n"
        "Адміністратор відповість тобі в особисті повідомлення."
    )
    
    for aid in ADMIN_IDS:
        try:
            await ctx.bot.send_message(
                chat_id=aid,
                text=(
                    f"❓ НОВЕ ПИТАННЯ\n"
                    f"══════════════\n\n"
                    f"👤 {name}\n"
                    f"🆔 {uid}\n"
                    f"📝 {text}\n"
                    f"🕐 {now_str()}"
                )
            )
        except Exception as e:
            log.warning("Не вдалось надіслати питання адміну %s: %s", aid, e)

async def cmd_about(update, ctx):
    if not await gate(update): return
    msg = (
        "ℹ️ ПРО БОТА\n"
        "════════════\n\n"
        "🤖 Бот моніторингу тривог\n"
        "📍 СМТ Ямпіль, Шепетівський р-н\n"
        "   Хмельницька область\n\n"
        "⚡ ФУНКЦІЇ:\n"
        "• Моніторинг тривог 24/7\n"
        "• Авто-публікація в канал\n"
        "• Карти тривог України\n"
        "• Реєстрація мешканців\n"
        "• Питання до адміністрації\n\n"
        f"⏱ Аптайм: {uptime()}\n"
        f"🚨 Тривог зафіксовано: {stats['alerts']}\n"
        f"✅ Відбоїв: {stats['allclear']}\n"
        f"📨 Повідомлень: {stats['messages']}\n"
        f"👥 Користувачів: {len(registered)}\n\n"
        "📡 API: ukrainealarm.com\n"
        "📢 @yampilnews\n\n"
        "Слава Україні! 🇺🇦"
    )
    m = update.message or update.callback_query.message
    await m.reply_text(msg)

async def cmd_help(update, ctx):
    if not await gate(update): return
    msg = (
        "📋 КОМАНДИ\n"
        "════════════\n\n"
        "/start        — меню\n"
        "/status       — статус тривоги\n"
        "/map          — карта тривог\n"
        "/shelters     — укриття\n"
        "/emergency    — екстрені номери\n"
        "/rules        — правила поведінки\n"
        "/contacts     — контакти\n"
        "/ask_question — питання адміну\n"
        "/about        — про бота\n"
        "/register     — реєстрація\n"
        "/myid         — мій ID\n"
    )
    await update.message.reply_text(msg)

async def cmd_myid(update, ctx):
    uid  = update.effective_user.id
    name = fname(update)
    role = (
        "🔐 Адміністратор" if is_admin(update)
        else ("✅ Зареєстрований" if is_reg(uid) else "❌ Не зареєстрований")
    )
    await update.message.reply_text(
        f"👤 {name}\n"
        f"🆔 ID: `{uid}`\n\n"
        f"{role}",
        parse_mode="Markdown"
    )

# ════════════════════════════════════════════
#           АДМІНІСТРУВАННЯ
# ════════════════════════════════════════════
async def cmd_admin(update, ctx):
    if not is_admin(update):
        await update.message.reply_text("🚫 Немає прав.")
        return
    
    qs = get_questions()
    msg = (
        f"🔐 АДМІН-ПАНЕЛЬ\n"
        f"════════════════\n\n"
        f"👤 {fname(update)}\n"
        f"🕐 {now_str()}\n\n"
        f"📊 ЗАРАЗ:\n"
        f"{'🔴 ТРИВОГА' if alert_active else '🟢 Спокійно'}\n"
        f"⏱ Аптайм: {uptime()}\n\n"
        f"📈 СТАТИСТИКА:\n"
        f"🚨 Тривог: {stats['alerts']}\n"
        f"✅ Відбоїв: {stats['allclear']}\n"
        f"📨 Повідомлень: {stats['messages']}\n"
        f"👥 Користувачів: {len(registered)}\n"
        f"🚫 Заблокованих: {len(banned)}\n"
        f"❓ Непрочитаних питань: {len(qs)}"
    )
    await update.message.reply_text(msg, reply_markup=kb_admin())

async def cmd_post(update, ctx):
    if not is_admin(update):
        await update.message.reply_text("🚫 Немає прав.")
        return
    text = " ".join(ctx.args).strip()
    if not text:
        await update.message.reply_text("✍️ /post <текст>")
        return
    await send_channel(
        ctx.bot,
        f"📢 ОГОЛОШЕННЯ\n══════════════\n\n{text}\n\n🕐 {now_str()}"
    )
    await update.message.reply_text("✅ Опубліковано в канал!")

async def cmd_ban(update, ctx):
    if not is_admin(update):
        await update.message.reply_text("🚫 Немає прав.")
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("❓ /ban <user_id>")
        return
    uid = int(ctx.args[0])
    ban_user(uid)
    await update.message.reply_text(f"🚫 Користувач {uid} заблокований.")

async def cmd_unban(update, ctx):
    if not is_admin(update):
        await update.message.reply_text("🚫 Немає прав.")
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("❓ /unban <user_id>")
        return
    uid = int(ctx.args[0])
    unban_user(uid)
    await update.message.reply_text(f"✅ Користувач {uid} розблокований.")

# ════════════════════════════════════════════
#           НОВІ КОМАНДИ
# ════════════════════════════════════════════
async def cmd_danger(update, ctx):
    if not await gate(update): return
    session = ctx.bot_data.get("session")
    uid = update.effective_user.id
    rid, rname = get_user_region(uid)
    try:
        headers = {"Authorization": ALERT_API_KEY}
        async with session.get(
            ALERT_API_URL, headers=headers,
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status == 200:
                data = await r.json()
                threats = []
                for region in data:
                    if TARGET_REGION in region.get("regionName", ""):
                        for alert in region.get("activeAlerts", []):
                            t = alert.get("type", "")
                            if t == "AIR":        threats.append("✈️ Авіаційна загроза")
                            elif t == "ARTILLERY": threats.append("💥 Артилерія")
                            elif t == "URBAN":    threats.append("🏙 Міські бої")
                            elif t == "CHEMICAL": threats.append("☢️ Хімічна небезпека")
                            else:                 threats.append(f"⚠️ {t}")
                if threats:
                    msg = f"⚠️ ПОТОЧНІ ЗАГРОЗИ\n══════════════════\n\n📍 {rname}\n🕐 {now_str()}\n\n" + "\n".join(threats)
                else:
                    msg = f"✅ ЗАГРОЗ НЕ ВИЯВЛЕНО\n══════════════════════\n\n📍 {rname}\n🕐 {now_str()}"
            else:
                msg = "❌ Не вдалось отримати дані."
    except Exception as e:
        log.error("danger: %s", e)
        msg = "❌ Помилка при отриманні даних."
    await update.message.reply_text(msg)

async def cmd_history(update, ctx):
    if not await gate(update): return
    hist = _load("/tmp/alert_history.json", [])
    if not hist:
        await update.message.reply_text(
            "📜 ІСТОРІЯ ТРИВОГ\n══════════════════\n\nПоки що тривог не зафіксовано."
        )
        return
    msg = "📜 ОСТАННІ ТРИВОГИ\n══════════════════\n\n"
    for i, h in enumerate(hist[-10:], 1):
        icon = "🔴" if h["type"] == "alert" else "✅"
        msg += f"{i}. {icon} {h['text']}\n   🕐 {h['time']}\n\n"
    await update.message.reply_text(msg)

async def cmd_pharmacy(update, ctx):
    if not await gate(update): return
    msg = (
        "💊 АПТЕКИ В СМТ ЯМПІЛЬ\n"
        "══════════════════════\n\n"
        "① Аптека №1\n"
        "   📍 вул. Центральна\n"
        "   🕐 Пн-Пт: 08:00–18:00\n"
        "   🕐 Сб: 09:00–14:00\n\n"
        "② Аптека №2\n"
        "   📍 вул. Незалежності\n"
        "   🕐 Пн-Пт: 08:00–19:00\n\n"
        "③ Аптека №3\n"
        "   📍 вул. Шкільна\n"
        "   🕐 Пн-Нд: 08:00–20:00\n\n"
        "═══════════════════════\n"
        "ℹ️ Уточнюйте години роботи\n"
        "📞 Швидка: 103"
    )
    await update.message.reply_text(msg)

async def cmd_schedule(update, ctx):
    if not await gate(update): return
    msg = (
        "🏛 РОЗКЛАД УСТАНОВ\n"
        "════════════════════\n\n"
        "🏛 ОТГ Адміністрація\n"
        "   📍 вул. Незалежності\n"
        "   🕐 Пн-Пт: 08:00–17:00\n"
        "   🕐 Обід: 12:00–13:00\n\n"
        "🏥 Амбулаторія\n"
        "   📍 вул. Медична\n"
        "   🕐 Пн-Пт: 07:30–19:00\n"
        "   🕐 Сб: 08:00–14:00\n\n"
        "🏫 Гімназія\n"
        "   📍 вул. Шкільна, 1\n"
        "   🕐 Пн-Пт: 08:00–17:00\n\n"
        "📮 Укрпошта\n"
        "   🕐 Пн-Пт: 09:00–17:00\n"
        "   🕐 Сб: 09:00–14:00\n\n"
        "🏦 ПриватБанк / Ощадбанк\n"
        "   🕐 Пн-Пт: 09:00–18:00"
    )
    await update.message.reply_text(msg)

async def cmd_transport(update, ctx):
    if not await gate(update): return
    msg = (
        "🚌 ТРАНСПОРТ\n"
        "═════════════\n\n"
        "🚌 Ямпіль → Шепетівка\n"
        "   🕐 07:00 | 09:30 | 12:00\n"
        "   🕐 14:30 | 16:00 | 18:00\n\n"
        "🚌 Шепетівка → Ямпіль\n"
        "   🕐 08:00 | 10:30 | 13:00\n"
        "   🕐 15:30 | 17:00 | 19:00\n\n"
        "🚌 Ямпіль → Хмельницький\n"
        "   🕐 06:30 | 11:00 | 15:00\n\n"
        "🚌 Хмельницький → Ямпіль\n"
        "   🕐 09:00 | 13:30 | 17:30\n\n"
        "═══════════════════════\n"
        "ℹ️ Розклад може змінюватись\n"
        "Уточнюйте у перевізника"
    )
    await update.message.reply_text(msg)

async def cmd_power(update, ctx):
    if not await gate(update): return
    msg = (
        "⚡ ГРАФІК ВІДКЛЮЧЕНЬ\n"
        "══════════════════════\n\n"
        f"📍 СМТ Ямпіль | 🕐 {now_str()}\n\n"
        "Черга 1 (вул. Центральна, Шкільна):\n"
        "   ⚡ 06:00–10:00 | 18:00–22:00\n\n"
        "Черга 2 (вул. Незалежності, Медична):\n"
        "   ⚡ 10:00–14:00 | 22:00–02:00\n\n"
        "Черга 3 (інші вулиці):\n"
        "   ⚡ 14:00–18:00 | 02:00–06:00\n\n"
        "═══════════════════════\n"
        "ℹ️ Графік може змінюватись"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🌐 Офіційний графік", url="https://oblenergo.km.ua"),
    ]])
    await update.message.reply_text(msg, reply_markup=kb)

async def cmd_broadcast(update, ctx):
    if not is_admin(update):
        await update.message.reply_text("🚫 Немає прав.")
        return
    text = " ".join(ctx.args).strip()
    if not text:
        await update.message.reply_text(
            "📣 Використання:\n/broadcast <текст>\n\n"
            f"Буде надіслано {len(registered)} користувачам."
        )
        return
    sent = 0
    failed = 0
    for uid in list(registered):
        try:
            await ctx.bot.send_message(
                chat_id=uid,
                text=f"📣 ПОВІДОМЛЕННЯ\n══════════════\n\n{text}\n\n🕐 {now_str()}"
            )
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
    await update.message.reply_text(
        f"✅ РОЗСИЛКУ ЗАВЕРШЕНО\n══════════════════\n\n"
        f"📨 Надіслано: {sent}\n❌ Помилок: {failed}"
    )

async def cmd_stats(update, ctx):
    if not is_admin(update):
        await update.message.reply_text("🚫 Немає прав.")
        return
    hist = _load("/tmp/alert_history.json", [])
    msg = (
        f"📊 ДЕТАЛЬНА СТАТИСТИКА\n"
        f"══════════════════════\n\n"
        f"⏱ Аптайм: {uptime()}\n"
        f"🕐 Час: {now_str()}\n\n"
        f"🚨 Тривог (сесія): {stats['alerts']}\n"
        f"✅ Відбоїв (сесія): {stats['allclear']}\n"
        f"📜 Тривог (всього): {sum(1 for h in hist if h.get('type')=='alert')}\n\n"
        f"📨 Повідомлень: {stats['messages']}\n"
        f"👥 Зареєстровано: {len(registered)}\n"
        f"🚫 Заблокованих: {len(banned)}\n\n"
        f"📡 API: ukrainealarm.com\n"
        f"🌍 Моніторинг: {TARGET_REGION}"
    )
    await update.message.reply_text(msg)
async def btn(update, ctx):
    q = update.callback_query
    await q.answer()
    d = q.data

    if   d == "status":    await cmd_status(update, ctx)
    elif d == "map":       await cmd_map(update, ctx)
    elif d == "shelters":  await cmd_shelters(update, ctx)
    elif d == "emergency": await cmd_emergency(update, ctx)
    elif d == "rules":     await cmd_rules(update, ctx)
    elif d == "contacts":  await cmd_contacts(update, ctx)
    elif d == "ask":       await cmd_ask_show(update, ctx)
    elif d == "region":    await cmd_region(update, ctx)
    elif d == "back_main":
        await q.message.reply_text("Головне меню:", reply_markup=kb_main())

    elif d.startswith("setreg_"):
        if not await gate(update): return
        rid = d.replace("setreg_", "")
        if rid not in REGIONS:
            await q.answer("❌ Невідомий регіон")
            return
        uid   = update.effective_user.id
        rname = REGIONS[rid]
        set_user_region(uid, rid)
        await q.answer(f"✅ {rname}")
        await q.message.reply_text(
            f"✅ РЕГІОН ЗМІНЕНО!\n"
            f"══════════════════\n\n"
            f"📍 {rname}\n\n"
            f"Тепер ти отримуватимеш статус тривоги для цього регіону.",
            reply_markup=kb_main()
        )

    elif d.startswith("regpage_"):
        page = int(d.replace("regpage_", ""))
        await q.message.edit_reply_markup(reply_markup=kb_regions(page))

    elif d == "a_post":
        if not is_admin(update): return
        await q.message.reply_text("✍️ /post <текст оголошення>")

    elif d == "a_talert":
        if not is_admin(update): return
        await send_channel(
            ctx.bot,
            f"‼️ ТРИВОГА!\n══════════\n\n"
            f"Станом на {now_str()}, в ОТГ Ямпіль\n"
            f"оголошена ПОВІТРЯНА ТРИВОГА!\n\n"
            f"⚠️ До укриття!\n🙏 Бережіть себе!\n\n"
            f"🔧 [ТЕСТ]"
        )
        await q.message.reply_text("✅ Тест тривоги надіслано!")

    elif d == "a_tclear":
        if not is_admin(update): return
        await send_channel(
            ctx.bot,
            f"✅ ВІДБІЙ!\n══════════\n\n"
            f"Станом на {now_str()}\n"
            f"оголошено ВІДБІЙ тривоги.\n\n"
            f"🟢 Спокійно\n\n"
            f"🔧 [ТЕСТ]"
        )
        await q.message.reply_text("✅ Тест відбою надіслано!")

    elif d == "a_users":
        if not is_admin(update): return
        await q.message.reply_text(
            f"👥 КОРИСТУВАЧІ\n"
            f"══════════════\n\n"
            f"✅ Зареєстровано: {len(registered)}\n"
            f"🚫 Заблокованих: {len(banned)}\n\n"
            f"🔧 /ban <id> — заблокувати\n"
            f"🔧 /unban <id> — розблокувати"
        )

    elif d == "a_questions":
        if not is_admin(update): return
        qs = get_questions()
        if not qs:
            await q.message.reply_text("❓ Питань поки немає.")
            return
        text = f"❓ ПИТАННЯ ({len(qs)} шт.)\n══════════════\n\n"
        for i, item in enumerate(qs[-5:], 1):
            text += f"{i}. 👤 {item['name']}\n   📝 {item['text']}\n   🕐 {item['time']}\n\n"
        _save(QUESTIONS_FILE, [])
        await q.message.reply_text(text)

    elif d in ("a_ban", "a_unban"):
        if not is_admin(update): return
        cmd = "/ban" if d == "a_ban" else "/unban"
        await q.message.reply_text(f"🔧 {cmd} <user_id>")

# ════════════════════════════════════════════
#           ФОНОВИЙ МОНІТОРИНГ
# ════════════════════════════════════════════
async def alarm_loop(bot: Bot, session: aiohttp.ClientSession):
    global alert_active
    log.info("⚡ Alert loop запущено")
    fail_count = 0
    
    while True:
        try:
            current = await fetch_alarm(session)
            fail_count = 0

            if alert_active is None:
                alert_active = current
                log.info("Початковий стан: %s", "ТРИВОГА" if current else "СПОКІЙНО")

            elif current and not alert_active:
                alert_active = True
                stats["alerts"] += 1
                log.info("🔴 ТРИВОГА!")
                hist = _load("/tmp/alert_history.json", [])
                hist.append({"type": "alert", "text": "Тривога оголошена", "time": now_str()})
                _save("/tmp/alert_history.json", hist[-50:])
                await send_channel(
                    bot,
                    f"‼️ ТРИВОГА!\n"
                    f"══════════════════\n\n"
                    f"Станом на {now_str()}\n"
                    f"в ОТГ селища Ямпіль\n"
                    f"оголошена ПОВІТРЯНА ТРИВОГА!\n\n"
                    f"⚠️ НЕГАЙНО ДО УКРИТТЯ!\n"
                    f"🙏 БЕРЕЖІТЬ СЕБЕ!"
                )
                await send_channel(
                    bot,
                    f"🗺 Карта тривог | {now_str()}\n{MAP_URL}",
                    photo=MAP_IMAGE_URL
                )

            elif not current and alert_active:
                alert_active = False
                stats["allclear"] += 1
                log.info("✅ ВІДБІЙ!")
                hist = _load("/tmp/alert_history.json", [])
                hist.append({"type": "clear", "text": "Відбій тривоги", "time": now_str()})
                _save("/tmp/alert_history.json", hist[-50:])
                await send_channel(
                    bot,
                    f"✅ ВІДБІЙ!\n"
                    f"══════════════════\n\n"
                    f"Станом на {now_str()}\n"
                    f"оголошено ВІДБІЙ\n"
                    f"повітряної тривоги.\n\n"
                    f"🟢 Спокійно!"
                )

        except Exception as e:
            fail_count += 1
            log.error("Alarm loop помилка #%d: %s", fail_count, e)
            if fail_count >= 5:
                log.critical("Занадто багато помилок! Чекаємо 5 хвилин...")
                await asyncio.sleep(300)
                fail_count = 0

        await asyncio.sleep(CHECK_INTERVAL)

# ════════════════════════════════════════════
#           WEBHOOK / HTTP СЕРВЕР
# ════════════════════════════════════════════
tg_app = None

async def webhook_handler(request):
    try:
        data = await request.json()
        upd  = Update.de_json(data, tg_app.bot)
        await tg_app.process_update(upd)
        return web.Response(status=200)
    except Exception as e:
        log.error("Webhook: %s", e)
        return web.Response(status=500)

async def health_handler(request):
    return web.json_response({
        "ok": True,
        "alert": alert_active,
        "uptime": uptime(),
        "time": now_str(),
        "users": len(registered)
    })

# ════════════════════════════════════════════
#                  MAIN
# ════════════════════════════════════════════
async def main():
    global tg_app
    stats["start"] = now_kyiv()

    tg_app = Application.builder().token(BOT_TOKEN).build()

    reg_handler = ConversationHandler(
        entry_points=[CommandHandler("register", cmd_register)],
        states={
            REG_NAME:    [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_name)],
            REG_PHONE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_phone)],
            REG_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_address)],
        },
        fallbacks=[CommandHandler("cancel", reg_cancel)],
    )

    tg_app.add_handler(reg_handler)
    tg_app.add_handler(CommandHandler("start",        cmd_start))
    tg_app.add_handler(CommandHandler("status",       cmd_status))
    tg_app.add_handler(CommandHandler("map",          cmd_map))
    tg_app.add_handler(CommandHandler("shelters",     cmd_shelters))
    tg_app.add_handler(CommandHandler("emergency",    cmd_emergency))
    tg_app.add_handler(CommandHandler("rules",        cmd_rules))
    tg_app.add_handler(CommandHandler("contacts",     cmd_contacts))
    tg_app.add_handler(CommandHandler("ask_question", cmd_ask))
    tg_app.add_handler(CommandHandler("about",        cmd_about))
    tg_app.add_handler(CommandHandler("help",         cmd_help))
    tg_app.add_handler(CommandHandler("region",       cmd_region))
    tg_app.add_handler(CommandHandler("myid",         cmd_myid))
    tg_app.add_handler(CommandHandler("admin",        cmd_admin))
    tg_app.add_handler(CommandHandler("post",         cmd_post))
    tg_app.add_handler(CommandHandler("ban",          cmd_ban))
    tg_app.add_handler(CommandHandler("unban",        cmd_unban))
    tg_app.add_handler(CommandHandler("danger",       cmd_danger))
    tg_app.add_handler(CommandHandler("history",      cmd_history))
    tg_app.add_handler(CommandHandler("pharmacy",     cmd_pharmacy))
    tg_app.add_handler(CommandHandler("schedule",     cmd_schedule))
    tg_app.add_handler(CommandHandler("transport",    cmd_transport))
    tg_app.add_handler(CommandHandler("power",        cmd_power))
    tg_app.add_handler(CommandHandler("broadcast",    cmd_broadcast))
    tg_app.add_handler(CommandHandler("stats",        cmd_stats))
    tg_app.add_handler(CallbackQueryHandler(btn))

    await tg_app.initialize()

    connector = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector) as session:
        tg_app.bot_data["session"] = session

        await tg_app.bot.set_webhook(url=WEBHOOK_URL, drop_pending_updates=True)
        log.info("🔗 Webhook: %s", WEBHOOK_URL)

        asyncio.create_task(alarm_loop(tg_app.bot, session))

        http = web.Application()
        http.router.add_post(WEBHOOK_PATH, webhook_handler)
        http.router.add_get("/health", health_handler)
        http.router.add_get("/", health_handler)

        runner = web.AppRunner(http)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", PORT).start()

        log.info("🚀 Сервер: порт %d", PORT)
        log.info("👥 Адмінів: %d | Користувачів: %d", len(ADMIN_IDS), len(registered))

        await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
