"""
╔══════════════════════════════════════════════════════╗
║     ЯМПІЛЬ АЛЕРТ БОТ  —  v3.0  FINAL EDITION        ║
║     СМТ Ямпіль, Шепетівський р-н, Хмельницька обл.   ║
╚══════════════════════════════════════════════════════╝
"""

import asyncio
import aiohttp
import logging
import os
import json
import re
import random
from datetime import datetime, timezone, timedelta

from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)
from telegram.error import TelegramError, RetryAfter, Forbidden
from aiohttp import web

# ╔══════════════════════════════════════════════════════╗
# ║                    КОНФІГУРАЦІЯ                      ║
# ╚══════════════════════════════════════════════════════╝
BOT_TOKEN      = os.getenv("BOT_TOKEN",     "8693341837:AAF2lK6bGR3uoLz1kfkZt8IjDQIF18YXHN8")
CHANNEL_ID     = os.getenv("CHANNEL_ID",    "@yampilnews")
ALERT_API_KEY  = os.getenv("ALERT_API_KEY", "b3de42c9:736017aa6745a605c155108e221d31a8")
ADMIN_IDS      = set(int(x) for x in os.getenv("ADMIN_IDS","").split(",") if x.strip().isdigit())
RENDER_URL     = os.getenv("RENDER_URL",    "https://mapyampilalert.onrender.com")
PORT           = int(os.getenv("PORT", 10000))

# UkraineAlarm API
UA_API         = "https://api.ukrainealarm.com/api/v3/alerts"
REGION_ID      = "26"   # Хмельницька область
CHECK_INTERVAL = 30

KYIV_TZ        = timezone(timedelta(hours=3))
WEBHOOK_URL    = f"{RENDER_URL}/webhook"
MAP_URL        = "https://alerts.in.ua/"
MAP_IMG        = "https://alerts.in.ua/map.png"

# Файли
F_USERS     = "/tmp/users.json"
F_BANNED    = "/tmp/banned.json"
F_REGIONS   = "/tmp/regions.json"
F_QUESTIONS = "/tmp/questions.json"
F_HISTORY   = "/tmp/history.json"
F_PROFILES  = "/tmp/profiles.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s │ %(levelname)s │ %(message)s")
log = logging.getLogger(__name__)

# ╔══════════════════════════════════════════════════════╗
# ║                    СТАН СИСТЕМИ                      ║
# ╚══════════════════════════════════════════════════════╝
alert_active: bool | None = None
stats = {"alerts":0, "clears":0, "msgs":0, "start": None}

REG_NAME, REG_PHONE, REG_ADDR = 1, 2, 3

# ╔══════════════════════════════════════════════════════╗
# ║                  РОБОТА З ФАЙЛАМИ                    ║
# ╚══════════════════════════════════════════════════════╝
def fload(path, default):
    try:
        if os.path.exists(path):
            with open(path) as f: return json.load(f)
    except: pass
    return default

def fsave(path, data):
    try:
        with open(path,"w") as f: json.dump(data, f, ensure_ascii=False)
    except Exception as e: log.warning("fsave %s: %s", path, e)

users    = set(fload(F_USERS, []))
banned   = set(fload(F_BANNED, []))
profiles = fload(F_PROFILES, {})

def is_reg(uid):   return uid in users
def is_ban(uid):   return uid in banned
def is_adm(upd):   return upd.effective_user.id in ADMIN_IDS

def reg(uid, profile=None):
    users.add(uid); fsave(F_USERS, list(users))
    if profile:
        profiles[str(uid)] = profile
        fsave(F_PROFILES, profiles)

def ban(uid):   banned.add(uid);    fsave(F_BANNED, list(banned))
def unban(uid): banned.discard(uid); fsave(F_BANNED, list(banned))

def get_profile(uid):   return profiles.get(str(uid), {})
def get_region(uid):
    r = fload(F_REGIONS, {})
    rid = r.get(str(uid), REGION_ID)
    return rid, REGIONS.get(rid, "Хмельницька обл.")

def set_region(uid, rid):
    r = fload(F_REGIONS, {})
    r[str(uid)] = rid; fsave(F_REGIONS, r)

def add_history(t, text):
    h = fload(F_HISTORY, [])
    h.append({"type":t, "text":text, "time":now_str()})
    fsave(F_HISTORY, h[-50:])

def get_questions(): return fload(F_QUESTIONS, [])
def clear_questions(): fsave(F_QUESTIONS, [])
def add_question(q):
    qs = get_questions(); qs.append(q); fsave(F_QUESTIONS, qs)

# ╔══════════════════════════════════════════════════════╗
# ║                    РЕГІОНИ УКРАЇНИ                   ║
# ╚══════════════════════════════════════════════════════╝
REGIONS = {
    "1":"Вінницька","2":"Волинська","3":"Дніпропетровська",
    "4":"Донецька","5":"Житомирська","6":"Закарпатська",
    "7":"Запорізька","8":"Івано-Франківська","9":"Київська",
    "10":"Кіровоградська","11":"Луганська","12":"Львівська",
    "13":"Миколаївська","14":"Одеська","15":"Полтавська",
    "16":"Рівненська","17":"Сумська","18":"Тернопільська",
    "19":"Харківська","20":"Херсонська","21":"Хмельницька",
    "22":"Черкаська","23":"Чернівецька","24":"Чернігівська",
    "25":"м. Київ","26":"Хмельницька (Шепетівський р-н)",
}

# ╔══════════════════════════════════════════════════════╗
# ║                  ДОПОМІЖНІ ФУНКЦІЇ                   ║
# ╚══════════════════════════════════════════════════════╝
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

def fname(upd):
    u = upd.effective_user
    return u.first_name if u and u.first_name else "друже"

def alert_txt():
    if alert_active is None: return "⏳ Перевіряємо..."
    return "🔴 АКТИВНА" if alert_active else "🟢 Спокійно"

def valid_phone(p): return len(re.sub(r'\D','',p)) >= 10

# ╔══════════════════════════════════════════════════════╗
# ║                    КЛАВІАТУРИ                        ║
# ╚══════════════════════════════════════════════════════╝
def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚨 Тривога",     callback_data="status"),
         InlineKeyboardButton("🗺 Карта",        callback_data="map")],
        [InlineKeyboardButton("⚠️ Загрози",     callback_data="danger"),
         InlineKeyboardButton("📜 Історія",      callback_data="history")],
        [InlineKeyboardButton("🏠 Укриття",     callback_data="shelters"),
         InlineKeyboardButton("📞 Екстренні",   callback_data="emergency")],
        [InlineKeyboardButton("🚌 Транспорт",   callback_data="transport"),
         InlineKeyboardButton("⚡ Світло",       callback_data="power")],
        [InlineKeyboardButton("💊 Аптеки",      callback_data="pharmacy"),
         InlineKeyboardButton("🏛 Розклад",      callback_data="schedule")],
        [InlineKeyboardButton("🌍 Мій регіон",  callback_data="region"),
         InlineKeyboardButton("❓ Запитання",   callback_data="ask")],
        [InlineKeyboardButton("📋 Правила",     callback_data="rules"),
         InlineKeyboardButton("ℹ️ Про бота",    callback_data="about")],
        [InlineKeyboardButton("📢 Канал",       url="https://t.me/yampilnews")],
    ])

def kb_status():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Оновити",  callback_data="status"),
        InlineKeyboardButton("🗺 Карта",    callback_data="map"),
        InlineKeyboardButton("🏠 Укриття", callback_data="shelters"),
    ]])

def kb_back():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Меню", callback_data="menu")]])

def kb_regions(page=0):
    items = list(REGIONS.items())
    per_page, start = 8, page * 8
    chunk = items[start:start+per_page]
    rows = []
    for i in range(0, len(chunk), 2):
        row = []
        for rid, rname in chunk[i:i+2]:
            short = rname.replace(" область","").replace(" обл.","")[:20]
            row.append(InlineKeyboardButton(short, callback_data=f"setreg_{rid}"))
        rows.append(row)
    nav = []
    if page > 0:          nav.append(InlineKeyboardButton("◀️", callback_data=f"rp_{page-1}"))
    if start+per_page < len(items): nav.append(InlineKeyboardButton("▶️", callback_data=f"rp_{page+1}"))
    if nav: rows.append(nav)
    rows.append([InlineKeyboardButton("🔙 Меню", callback_data="menu")])
    return InlineKeyboardMarkup(rows)

def kb_admin():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Оголошення",   callback_data="a_post"),
         InlineKeyboardButton("📣 Розсилка",     callback_data="a_broadcast")],
        [InlineKeyboardButton("🔴 Тест тривога", callback_data="a_talert"),
         InlineKeyboardButton("🟢 Тест відбій",  callback_data="a_tclear")],
        [InlineKeyboardButton("👥 Користувачі",  callback_data="a_users"),
         InlineKeyboardButton("❓ Питання",      callback_data="a_questions")],
        [InlineKeyboardButton("📊 Статистика",   callback_data="a_stats"),
         InlineKeyboardButton("📜 Тривоги",      callback_data="a_history")],
        [InlineKeyboardButton("🚫 /ban <id>",    callback_data="a_ban"),
         InlineKeyboardButton("✅ /unban <id>",  callback_data="a_unban")],
    ])

# ╔══════════════════════════════════════════════════════╗
# ║                    UKRAINE ALARM API                 ║
# ╚══════════════════════════════════════════════════════╝
async def fetch_alarm(session: aiohttp.ClientSession, region_id: str = REGION_ID) -> bool:
    try:
        headers = {"Authorization": ALERT_API_KEY}
        async with session.get(
            f"{UA_API}/{region_id}",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status == 200:
                data = await r.json()
                for item in data:
                    for alert in item.get("activeAlerts", []):
                        if alert.get("type") == "AIR":
                            return True
                return False
            log.error("API %d: %s", r.status, await r.text())
            return alert_active or False
    except asyncio.TimeoutError:
        log.warning("API timeout")
        return alert_active or False
    except Exception as e:
        log.error("API: %s", e)
        return alert_active or False

async def send_ch(bot: Bot, text: str, photo: str = None):
    try:
        if photo:
            await bot.send_photo(chat_id=CHANNEL_ID, photo=photo, caption=text)
        else:
            await bot.send_message(chat_id=CHANNEL_ID, text=text)
        stats["msgs"] += 1
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after)
    except TelegramError as e:
        log.error("TG: %s", e)

# ╔══════════════════════════════════════════════════════╗
# ║                   ПЕРЕВІРКА ДОСТУПУ                  ║
# ╚══════════════════════════════════════════════════════╝
async def gate(upd) -> bool:
    uid = upd.effective_user.id
    if is_ban(uid):
        await upd.message.reply_text("🚫 Вас заблоковано.")
        return False
    if not is_reg(uid) and not is_adm(upd):
        await upd.message.reply_text(
            "👋 Привіт! Для доступу потрібна реєстрація.\n\n▶️ /register"
        )
        return False
    return True

# ╔══════════════════════════════════════════════════════╗
# ║                     РЕЄСТРАЦІЯ                       ║
# ╚══════════════════════════════════════════════════════╝
async def cmd_register(upd, ctx):
    uid = upd.effective_user.id
    if is_reg(uid):
        await upd.message.reply_text("✅ Ти вже зареєстрований!\n\n▶️ /start"); return ConversationHandler.END
    if is_ban(uid):
        await upd.message.reply_text("🚫 Реєстрація недоступна."); return ConversationHandler.END
    await upd.message.reply_text(
        "📋 РЕЄСТРАЦІЯ  [ 1 / 3 ]\n"
        "══════════════════════\n\n"
        "👤 Як тебе звати?"
    )
    return REG_NAME

async def rg_name(upd, ctx):
    ctx.user_data["name"] = upd.message.text.strip()
    await upd.message.reply_text(
        "📋 РЕЄСТРАЦІЯ  [ 2 / 3 ]\n"
        "══════════════════════\n\n"
        "📱 Номер телефону?\n"
        "Приклад: +380 95 123 45 67"
    )
    return REG_PHONE

async def rg_phone(upd, ctx):
    phone = upd.message.text.strip()
    if not valid_phone(phone):
        await upd.message.reply_text("❌ Невірний формат. Спробуй ще раз:"); return REG_PHONE
    ctx.user_data["phone"] = phone
    await upd.message.reply_text(
        "📋 РЕЄСТРАЦІЯ  [ 3 / 3 ]\n"
        "══════════════════════\n\n"
        "🏠 Твоя адреса?\n"
        "Приклад: вул. Центральна, 5"
    )
    return REG_ADDR

async def rg_addr(upd, ctx):
    ctx.user_data["address"] = upd.message.text.strip()
    uid  = upd.effective_user.id
    name = upd.effective_user.username or ctx.user_data["name"]
    reg(uid, {
        "name":    ctx.user_data["name"],
        "phone":   ctx.user_data["phone"],
        "address": ctx.user_data["address"],
        "tg":      name,
        "reg_at":  now_str(),
    })
    await upd.message.reply_text(
        "✅ РЕЄСТРАЦІЯ ЗАВЕРШЕНА!\n"
        "══════════════════════\n\n"
        f"👤 {ctx.user_data['name']}\n"
        f"📱 {ctx.user_data['phone']}\n"
        f"🏠 {ctx.user_data['address']}\n\n"
        "Ласкаво просимо! ▶️ /start"
    )
    return ConversationHandler.END

async def rg_cancel(upd, ctx):
    await upd.message.reply_text("❌ Реєстрацію скасовано."); return ConversationHandler.END

# ╔══════════════════════════════════════════════════════╗
# ║                      КОМАНДИ                         ║
# ╚══════════════════════════════════════════════════════╝
async def cmd_start(upd, ctx):
    if not await gate(upd): return
    name = fname(upd)
    rid, rname = get_region(upd.effective_user.id)
    st = alert_txt()

    msg = (
        f"{hello()}, {name}!\n"
        f"══════════════════════\n\n"
        f"📍 СМТ Ямпіль · Шепетівський р-н\n"
        f"🌍 Мій регіон: {rname}\n\n"
        f"🚨 Тривога: {st}\n"
    )
    if alert_active:
        tips = [
            "🏃 Рухайтесь до укриття!",
            "📵 Вимкніть світло!",
            "🧳 Візьміть тривожну валізу!"
        ]
        msg += f"\n{random.choice(tips)}\n🙏 Бережіть себе!\n"
    msg += "\n══════════════════════\nОберіть дію:"
    await upd.message.reply_text(msg, reply_markup=kb_main())
    stats["msgs"] += 1

async def cmd_status(upd, ctx):
    if not await gate(upd): return
    uid = upd.effective_user.id
    rid, rname = get_region(uid)
    st  = alert_txt()
    msg = (
        f"🛡 СТАТУС ТРИВОГИ\n"
        f"══════════════════\n\n"
        f"📍 {rname}\n"
        f"🕐 {now_str()}\n\n"
        f"Стан: {st}\n"
    )
    if alert_active:
        msg += "\n⚠️ ТРИВОГА АКТИВНА!\n🏃 До укриття!\n🙏 Бережіть себе!"
    m = upd.message or upd.callback_query.message
    await m.reply_text(msg, reply_markup=kb_status())

async def cmd_map(upd, ctx):
    if not await gate(upd): return
    m = upd.message or (upd.callback_query.message if upd.callback_query else None)
    uid = upd.effective_user.id if upd.effective_user else 0
    _, rname = get_region(uid)
    cap = (
        f"🗺 КАРТА ТРИВОГ УКРАЇНИ\n"
        f"══════════════════════\n\n"
        f"🕐 {now_str()}\n"
        f"📍 {rname}: {alert_txt()}\n\n"
        f"🔴 тривога  |  🟢 спокійно"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🌐 Онлайн", url=MAP_URL),
        InlineKeyboardButton("🔄 Оновити", callback_data="map"),
    ]])
    try:
        await m.reply_photo(photo=MAP_IMG, caption=cap, reply_markup=kb)
    except Exception:
        await m.reply_text(f"{cap}\n\n🔗 {MAP_URL}", reply_markup=kb)

async def cmd_danger(upd, ctx):
    if not await gate(upd): return
    session = ctx.bot_data.get("session")
    uid = upd.effective_user.id if upd.effective_user else 0
    rid, rname = get_region(uid)
    try:
        headers = {"Authorization": ALERT_API_KEY}
        async with session.get(f"{UA_API}/{rid}", headers=headers,
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            threats = []
            if r.status == 200:
                data = await r.json()
                TYPE_MAP = {
                    "AIR":       "✈️ Авіаційна загроза",
                    "ARTILLERY": "💥 Артилерія",
                    "URBAN":     "🏙 Міські бої",
                    "CHEMICAL":  "☢️ Хімічна небезпека",
                    "NUCLEAR":   "☢️ Ядерна небезпека",
                }
                for item in data:
                    for alert in item.get("activeAlerts", []):
                        t = alert.get("type","")
                        threats.append(TYPE_MAP.get(t, f"⚠️ {t}"))
        if threats:
            msg = f"⚠️ АКТИВНІ ЗАГРОЗИ\n══════════════════\n\n📍 {rname}\n🕐 {now_str()}\n\n" + "\n".join(threats)
        else:
            msg = f"✅ ЗАГРОЗ НЕ ВИЯВЛЕНО\n══════════════════════\n\n📍 {rname}\n🕐 {now_str()}"
    except Exception as e:
        msg = f"❌ Помилка при отриманні даних.\n{e}"
    m = upd.message or upd.callback_query.message
    await m.reply_text(msg, reply_markup=kb_back())

async def cmd_history(upd, ctx):
    if not await gate(upd): return
    hist = fload(F_HISTORY, [])
    if not hist:
        msg = "📜 ІСТОРІЯ ТРИВОГ\n══════════════════\n\nЩе немає записів."
    else:
        msg = f"📜 ОСТАННІ ТРИВОГИ ({len(hist)} шт.)\n══════════════════\n\n"
        for i, h in enumerate(reversed(hist[-10:]), 1):
            icon = "🔴" if h["type"] == "alert" else "✅"
            msg += f"{i}. {icon} {h['text']}\n   🕐 {h['time']}\n\n"
    m = upd.message or upd.callback_query.message
    await m.reply_text(msg, reply_markup=kb_back())

async def cmd_shelters(upd, ctx):
    if not await gate(upd): return
    msg = (
        "🏠 УКРИТТЯ — СМТ ЯМПІЛЬ\n"
        "═══════════════════════\n\n"
        "① Підвал гімназії\n   📍 вул. Шкільна, 1\n\n"
        "② Будинок культури\n   📍 вул. Центральна\n\n"
        "③ Амбулаторія\n   📍 вул. Медична\n\n"
        "④ Адміністрація ОТГ\n   📍 вул. Незалежності\n\n"
        "═══════════════════════\n"
        "⚠️ При тривозі — до найближчого!\n"
        "📞 ДСНС: 104"
    )
    m = upd.message or upd.callback_query.message
    await m.reply_text(msg, reply_markup=kb_back())

async def cmd_emergency(upd, ctx):
    if not await gate(upd): return
    msg = (
        "📞 ЕКСТРЕНІ СЛУЖБИ\n"
        "════════════════════\n\n"
        "🚒  101  Пожежна\n"
        "🚔  102  Поліція\n"
        "🚑  103  Швидка\n"
        "🛡  104  ДСНС\n"
        "☎️  112  Єдиний\n\n"
        "════════════════════\n"
        "🇺🇦  1580  МО України\n\n"
        "📻 Офіційні джерела!"
    )
    m = upd.message or upd.callback_query.message
    await m.reply_text(msg, reply_markup=kb_back())

async def cmd_rules(upd, ctx):
    if not await gate(upd): return
    msg = (
        "📋 ПРАВИЛА ПІД ЧАС ТРИВОГИ\n"
        "═══════════════════════════\n\n"
        "① Зупиніться та зберіть речі\n"
        "② Перейдіть до укриття\n"
        "③ Якщо немає — біля капітальної стіни\n"
        "④ Подалі від вікон\n"
        "⑤ Вимкніть газ та прилади\n"
        "⑥ Документи, ліки, телефон — з собою\n"
        "⑦ Не виходьте до відбою\n"
        "⑧ Допоможіть сусідам\n\n"
        "═══════════════════════════\n"
        "💥 Після вибуху поряд:\n"
        "• Ляжте, прикрийте голову\n"
        "• Відійдіть від вікон\n"
        "• Чекайте рятувальників\n\n"
        "🙏 Бережіть себе!"
    )
    m = upd.message or upd.callback_query.message
    await m.reply_text(msg, reply_markup=kb_back())

async def cmd_transport(upd, ctx):
    if not await gate(upd): return
    msg = (
        "🚌 ТРАНСПОРТ\n"
        "═════════════\n\n"
        "Ямпіль → Шепетівка\n"
        "  07:00 · 09:30 · 12:00 · 15:30 · 18:00\n\n"
        "Шепетівка → Ямпіль\n"
        "  08:00 · 10:30 · 13:00 · 16:30 · 19:00\n\n"
        "Ямпіль → Хмельницький\n"
        "  06:30 · 11:00 · 15:00\n\n"
        "Хмельницький → Ямпіль\n"
        "  09:00 · 13:30 · 17:30\n\n"
        "═══════════════════════\n"
        "ℹ️ Уточнюйте у перевізника"
    )
    m = upd.message or upd.callback_query.message
    await m.reply_text(msg, reply_markup=kb_back())

async def cmd_power(upd, ctx):
    if not await gate(upd): return
    msg = (
        "⚡ ГРАФІК ВІДКЛЮЧЕНЬ\n"
        f"════════════════════\n\n"
        f"📍 СМТ Ямпіль\n"
        f"🕐 {now_str()}\n\n"
        "Черга 1 (Центральна, Шкільна)\n"
        "  ⚡ 06:00–10:00 | 18:00–22:00\n\n"
        "Черга 2 (Незалежності, Медична)\n"
        "  ⚡ 10:00–14:00 | 22:00–02:00\n\n"
        "Черга 3 (інші вулиці)\n"
        "  ⚡ 14:00–18:00 | 02:00–06:00\n\n"
        "════════════════════\n"
        "ℹ️ Графік може змінюватись"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🌐 oblenergo.km.ua", url="https://oblenergo.km.ua"),
        InlineKeyboardButton("🔙 Меню", callback_data="menu"),
    ]])
    m = upd.message or upd.callback_query.message
    await m.reply_text(msg, reply_markup=kb)

async def cmd_pharmacy(upd, ctx):
    if not await gate(upd): return
    msg = (
        "💊 АПТЕКИ — СМТ ЯМПІЛЬ\n"
        "══════════════════════\n\n"
        "① вул. Центральна\n"
        "   🕐 Пн-Пт: 08:00–18:00 · Сб: 09:00–14:00\n\n"
        "② вул. Незалежності\n"
        "   🕐 Пн-Пт: 08:00–19:00\n\n"
        "③ вул. Шкільна\n"
        "   🕐 Щодня: 08:00–20:00\n\n"
        "══════════════════════\n"
        "📞 Швидка: 103"
    )
    m = upd.message or upd.callback_query.message
    await m.reply_text(msg, reply_markup=kb_back())

async def cmd_schedule(upd, ctx):
    if not await gate(upd): return
    msg = (
        "🏛 РОЗКЛАД УСТАНОВ\n"
        "════════════════════\n\n"
        "🏛 ОТГ Адміністрація\n"
        "   Пн-Пт: 08:00–17:00 | Обід: 12–13\n\n"
        "🏥 Амбулаторія\n"
        "   Пн-Пт: 07:30–19:00 | Сб: 08–14\n\n"
        "🏫 Гімназія\n"
        "   Пн-Пт: 08:00–17:00\n\n"
        "📮 Укрпошта\n"
        "   Пн-Пт: 09:00–17:00 | Сб: 09–14\n\n"
        "🏦 Банки\n"
        "   Пн-Пт: 09:00–18:00"
    )
    m = upd.message or upd.callback_query.message
    await m.reply_text(msg, reply_markup=kb_back())

async def cmd_region(upd, ctx):
    if not await gate(upd): return
    uid = upd.effective_user.id if upd.effective_user else 0
    rid, rname = get_region(uid)
    msg = (
        f"🌍 МІЙ РЕГІОН\n"
        f"══════════════\n\n"
        f"Зараз: 📍 {rname}\n\n"
        f"Обери інший регіон:"
    )
    m = upd.message or upd.callback_query.message
    await m.reply_text(msg, reply_markup=kb_regions())

async def cmd_ask_show(upd, ctx):
    if not await gate(upd): return
    m = upd.callback_query.message
    await m.reply_text(
        "❓ ПИТАННЯ АДМІНІСТРАЦІЇ\n"
        "═══════════════════════\n\n"
        "Твоє питання отримає адміністратор\n"
        "і відповість у приватні повідомлення.\n\n"
        "▶️ /ask_question <текст>\n\n"
        "Приклад:\n"
        "/ask_question Де найближче укриття?"
    )

async def cmd_ask(upd, ctx):
    if not await gate(upd): return
    text = " ".join(ctx.args).strip()
    if not text:
        await upd.message.reply_text("❓ /ask_question <текст>"); return
    uid  = upd.effective_user.id
    name = fname(upd)
    add_question({"uid":uid,"name":name,"text":text,"time":now_str()})
    await upd.message.reply_text(
        "✅ Питання надіслано!\n\nАдміністратор відповість у приватні повідомлення."
    )
    for aid in ADMIN_IDS:
        try:
            await ctx.bot.send_message(
                chat_id=aid,
                text=f"❓ НОВЕ ПИТАННЯ\n══════════════\n\n👤 {name} ({uid})\n📝 {text}\n🕐 {now_str()}"
            )
        except: pass

async def cmd_about(upd, ctx):
    if not await gate(upd): return
    uid = upd.effective_user.id if upd.effective_user else 0
    _, rname = get_region(uid)
    msg = (
        "ℹ️ ЯМПІЛЬ АЛЕРТ БОТ\n"
        "════════════════════\n\n"
        "📍 СМТ Ямпіль\n"
        "   Шепетівський р-н · Хмельницька обл.\n\n"
        "⚡ ФУНКЦІЇ:\n"
        "✅ Моніторинг тривог 24/7\n"
        "✅ Авто-публікація в канал\n"
        "✅ Карта тривог в реальному часі\n"
        "✅ Вибір регіону для кожного\n"
        "✅ Питання до адміністрації\n"
        "✅ Транспорт, аптеки, розклад\n"
        "✅ Графік відключень\n"
        "✅ Розсилка адміном\n\n"
        f"⏱ Аптайм: {uptime()}\n"
        f"🚨 Тривог: {stats['alerts']}\n"
        f"📨 Повідомлень: {stats['msgs']}\n"
        f"👥 Користувачів: {len(users)}\n"
        f"🌍 Регіон: {rname}\n\n"
        "📡 API: ukrainealarm.com\n"
        "📢 @yampilnews\n\n"
        "Слава Україні! 🇺🇦"
    )
    m = upd.message or upd.callback_query.message
    await m.reply_text(msg, reply_markup=kb_back())

async def cmd_help(upd, ctx):
    if not await gate(upd): return
    msg = (
        "📋 ВСІ КОМАНДИ\n"
        "═══════════════\n\n"
        "👤 ЗАГАЛЬНІ:\n"
        "/start        — меню\n"
        "/status       — статус тривоги\n"
        "/map          — карта тривог\n"
        "/danger       — поточні загрози\n"
        "/history      — історія тривог\n"
        "/shelters     — укриття\n"
        "/emergency    — екстрені номери\n"
        "/rules        — правила поведінки\n"
        "/transport    — розклад транспорту\n"
        "/power        — графік відключень\n"
        "/pharmacy     — аптеки\n"
        "/schedule     — розклад установ\n"
        "/region       — змінити регіон\n"
        "/ask_question — питання адміну\n"
        "/about        — про бота\n"
        "/myid         — мій Telegram ID\n"
        "/register     — реєстрація\n\n"
        "🔐 АДМІН:\n"
        "/admin        — панель адміна\n"
        "/post         — оголошення в канал\n"
        "/broadcast    — розсилка всім\n"
        "/stats        — детальна статистика\n"
        "/ban <id>     — заблокувати\n"
        "/unban <id>   — розблокувати"
    )
    await upd.message.reply_text(msg)

async def cmd_myid(upd, ctx):
    uid  = upd.effective_user.id
    name = fname(upd)
    role = "🔐 Адміністратор" if is_adm(upd) else ("✅ Зареєстрований" if is_reg(uid) else "❌ Не зареєстрований")
    p    = get_profile(uid)
    msg  = f"👤 {name}\n🆔 ID: `{uid}`\n\n{role}"
    if p: msg += f"\n\n📱 {p.get('phone','—')}\n🏠 {p.get('address','—')}"
    await upd.message.reply_text(msg, parse_mode="Markdown")

# ╔══════════════════════════════════════════════════════╗
# ║                  АДМІН КОМАНДИ                       ║
# ╚══════════════════════════════════════════════════════╝
async def cmd_admin(upd, ctx):
    if not is_adm(upd):
        await upd.message.reply_text("🚫 Немає прав."); return
    qs = get_questions()
    msg = (
        f"🔐 АДМІН-ПАНЕЛЬ  v3.0\n"
        f"══════════════════════\n\n"
        f"👤 {fname(upd)}\n"
        f"🕐 {now_str()}\n\n"
        f"{'🔴 ТРИВОГА АКТИВНА' if alert_active else '🟢 Спокійно'}\n"
        f"⏱ Аптайм: {uptime()}\n\n"
        f"📈 СТАТИСТИКА:\n"
        f"🚨 Тривог: {stats['alerts']}\n"
        f"✅ Відбоїв: {stats['clears']}\n"
        f"📨 Повідомлень: {stats['msgs']}\n"
        f"👥 Зареєстровано: {len(users)}\n"
        f"🚫 Заблокованих: {len(banned)}\n"
        f"❓ Питань: {len(qs)}"
    )
    await upd.message.reply_text(msg, reply_markup=kb_admin())

async def cmd_post(upd, ctx):
    if not is_adm(upd):
        await upd.message.reply_text("🚫 Немає прав."); return
    text = " ".join(ctx.args).strip()
    if not text:
        await upd.message.reply_text("✍️ /post <текст>"); return
    await send_ch(ctx.bot, f"📢 ОГОЛОШЕННЯ\n══════════════\n\n{text}\n\n🕐 {now_str()}")
    await upd.message.reply_text("✅ Опубліковано!")

async def cmd_broadcast(upd, ctx):
    if not is_adm(upd):
        await upd.message.reply_text("🚫 Немає прав."); return
    text = " ".join(ctx.args).strip()
    if not text:
        await upd.message.reply_text(f"📣 /broadcast <текст>\n\nНадішле {len(users)} користувачам."); return
    sent = failed = 0
    for uid in list(users):
        try:
            await ctx.bot.send_message(
                chat_id=uid,
                text=f"📣 ПОВІДОМЛЕННЯ\n══════════════\n\n{text}\n\n🕐 {now_str()}"
            )
            sent += 1
            await asyncio.sleep(0.05)
        except Forbidden:
            failed += 1
        except Exception:
            failed += 1
    await upd.message.reply_text(f"✅ Розсилка завершена!\n\n📨 Надіслано: {sent}\n❌ Помилок: {failed}")

async def cmd_stats(upd, ctx):
    if not is_adm(upd):
        await upd.message.reply_text("🚫 Немає прав."); return
    hist = fload(F_HISTORY, [])
    msg = (
        f"📊 ДЕТАЛЬНА СТАТИСТИКА\n"
        f"══════════════════════\n\n"
        f"⏱ Аптайм: {uptime()}\n"
        f"🕐 {now_str()}\n\n"
        f"🚨 Тривог (сесія): {stats['alerts']}\n"
        f"✅ Відбоїв (сесія): {stats['clears']}\n"
        f"📜 Тривог (всього): {sum(1 for h in hist if h.get('type')=='alert')}\n"
        f"📨 Повідомлень: {stats['msgs']}\n\n"
        f"👥 Зареєстровано: {len(users)}\n"
        f"🚫 Заблокованих: {len(banned)}\n\n"
        f"📡 API: ukrainealarm.com\n"
        f"🌍 Регіон ID: {REGION_ID}"
    )
    await upd.message.reply_text(msg)

async def cmd_ban(upd, ctx):
    if not is_adm(upd): await upd.message.reply_text("🚫"); return
    if not ctx.args or not ctx.args[0].isdigit():
        await upd.message.reply_text("/ban <user_id>"); return
    uid = int(ctx.args[0]); ban(uid)
    await upd.message.reply_text(f"🚫 {uid} заблокований.")

async def cmd_unban(upd, ctx):
    if not is_adm(upd): await upd.message.reply_text("🚫"); return
    if not ctx.args or not ctx.args[0].isdigit():
        await upd.message.reply_text("/unban <user_id>"); return
    uid = int(ctx.args[0]); unban(uid)
    await upd.message.reply_text(f"✅ {uid} розблокований.")

# ╔══════════════════════════════════════════════════════╗
# ║                 ОБРОБКА КНОПОК                       ║
# ╚══════════════════════════════════════════════════════╝
async def btn(upd, ctx):
    q = upd.callback_query
    await q.answer()
    d = q.data

    dispatch = {
        "status":    cmd_status,
        "map":       cmd_map,
        "danger":    cmd_danger,
        "history":   cmd_history,
        "shelters":  cmd_shelters,
        "emergency": cmd_emergency,
        "rules":     cmd_rules,
        "transport": cmd_transport,
        "power":     cmd_power,
        "pharmacy":  cmd_pharmacy,
        "schedule":  cmd_schedule,
        "region":    cmd_region,
        "ask":       cmd_ask_show,
        "about":     cmd_about,
    }

    if d in dispatch:
        await dispatch[d](upd, ctx)

    elif d == "menu":
        uid = upd.effective_user.id
        _, rname = get_region(uid)
        st = alert_txt()
        await q.message.reply_text(
            f"🏠 ГОЛОВНЕ МЕНЮ\n══════════════\n\n"
            f"📍 {rname}\n🚨 {st}",
            reply_markup=kb_main()
        )

    elif d.startswith("setreg_"):
        rid = d[7:]
        if rid not in REGIONS: await q.answer("❌ Невідомий регіон"); return
        uid = upd.effective_user.id
        set_region(uid, rid)
        await q.answer(f"✅ {REGIONS[rid]}")
        await q.message.reply_text(
            f"✅ РЕГІОН ЗМІНЕНО!\n══════════════════\n\n"
            f"📍 {REGIONS[rid]}\n\n"
            "Тепер статус тривоги для цього регіону.",
            reply_markup=kb_main()
        )

    elif d.startswith("rp_"):
        await q.message.edit_reply_markup(reply_markup=kb_regions(int(d[3:])))

    elif d == "a_post" and is_adm(upd):
        await q.message.reply_text("✍️ /post <текст>")

    elif d == "a_broadcast" and is_adm(upd):
        await q.message.reply_text(f"📣 /broadcast <текст>\n\nНадішле {len(users)} користувачам.")

    elif d == "a_talert" and is_adm(upd):
        await send_ch(ctx.bot,
            f"‼️ ТРИВОГА!\n══════════\n\n"
            f"Станом на {now_str()}\n"
            f"в ОТГ Ямпіль оголошена\n"
            f"ПОВІТРЯНА ТРИВОГА!\n\n"
            f"⚠️ До укриття!\n🙏 Бережіть себе!\n\n🔧 [ТЕСТ]")
        await q.message.reply_text("✅ Тест надіслано!")

    elif d == "a_tclear" and is_adm(upd):
        await send_ch(ctx.bot,
            f"✅ ВІДБІЙ!\n══════════\n\n"
            f"Станом на {now_str()}\n"
            f"оголошено відбій тривоги.\n\n"
            f"🟢 Спокійно!\n\n🔧 [ТЕСТ]")
        await q.message.reply_text("✅ Тест надіслано!")

    elif d == "a_users" and is_adm(upd):
        await q.message.reply_text(
            f"👥 КОРИСТУВАЧІ\n══════════════\n\n"
            f"✅ Зареєстровано: {len(users)}\n"
            f"🚫 Заблокованих: {len(banned)}"
        )

    elif d == "a_questions" and is_adm(upd):
        qs = get_questions()
        if not qs:
            await q.message.reply_text("❓ Питань немає."); return
        msg = f"❓ ПИТАННЯ ({len(qs)})\n══════════\n\n"
        for i, item in enumerate(qs[-5:], 1):
            msg += f"{i}. 👤 {item['name']}\n   📝 {item['text']}\n   🕐 {item['time']}\n\n"
        clear_questions()
        await q.message.reply_text(msg)

    elif d == "a_stats" and is_adm(upd):
        await cmd_stats(upd, ctx)

    elif d == "a_history" and is_adm(upd):
        await cmd_history(upd, ctx)

    elif d == "a_ban" and is_adm(upd):
        await q.message.reply_text("🚫 /ban <user_id>")

    elif d == "a_unban" and is_adm(upd):
        await q.message.reply_text("✅ /unban <user_id>")

# ╔══════════════════════════════════════════════════════╗
# ║                  ФОНОВИЙ МОНІТОРИНГ                  ║
# ╚══════════════════════════════════════════════════════╝
async def alarm_loop(bot: Bot, session: aiohttp.ClientSession):
    global alert_active
    fail_count = 0
    log.info("⚡ Alarm loop запущено | Регіон ID: %s", REGION_ID)

    while True:
        try:
            current = await fetch_alarm(session, REGION_ID)
            fail_count = 0

            if alert_active is None:
                alert_active = current
                log.info("Початковий стан: %s", "ТРИВОГА" if current else "СПОКІЙНО")

            elif current and not alert_active:
                alert_active = True
                stats["alerts"] += 1
                add_history("alert", "Тривога оголошена")
                log.info("🔴 ТРИВОГА!")
                await send_ch(
                    bot,
                    f"‼️ ТРИВОГА!\n"
                    f"══════════════════\n\n"
                    f"Станом на {now_str()}\n"
                    f"в ОТГ селища Ямпіль\n"
                    f"оголошена ПОВІТРЯНА ТРИВОГА!\n\n"
                    f"⚠️ НЕГАЙНО ДО УКРИТТЯ!\n"
                    f"🙏 БЕРЕЖІТЬ СЕБЕ!"
                )
                await send_ch(
                    bot,
                    f"🗺 Карта тривог | {now_str()}\n{MAP_URL}",
                    photo=MAP_IMG
                )

            elif not current and alert_active:
                alert_active = False
                stats["clears"] += 1
                add_history("clear", "Відбій оголошено")
                log.info("✅ ВІДБІЙ!")
                await send_ch(
                    bot,
                    f"✅ ВІДБІЙ!\n"
                    f"══════════════════\n\n"
                    f"Станом на {now_str()}\n"
                    f"оголошено ВІДБІЙ тривоги.\n\n"
                    f"🟢 Спокійно!"
                )

        except Exception as e:
            fail_count += 1
            log.error("Alarm loop #%d: %s", fail_count, e)
            if fail_count >= 5:
                log.critical("5 помилок підряд! Пауза 5 хвилин...")
                await asyncio.sleep(300)
                fail_count = 0

        await asyncio.sleep(CHECK_INTERVAL)

# ╔══════════════════════════════════════════════════════╗
# ║               WEBHOOK / HTTP СЕРВЕР                  ║
# ╚══════════════════════════════════════════════════════╝
tg_app = None

async def wh_handler(request):
    try:
        data = await request.json()
        upd  = Update.de_json(data, tg_app.bot)
        await tg_app.process_update(upd)
        return web.Response(status=200)
    except Exception as e:
        log.error("WH: %s", e)
        return web.Response(status=500)

async def health(request):
    return web.json_response({
        "ok":      True,
        "alert":   alert_active,
        "uptime":  uptime(),
        "time":    now_str(),
        "users":   len(users),
        "alerts":  stats["alerts"],
        "version": "3.0"
    })

# ╔══════════════════════════════════════════════════════╗
# ║                       MAIN                           ║
# ╚══════════════════════════════════════════════════════╝
async def main():
    global tg_app
    stats["start"] = now_kyiv()

    tg_app = Application.builder().token(BOT_TOKEN).build()

    reg_handler = ConversationHandler(
        entry_points=[CommandHandler("register", cmd_register)],
        states={
            REG_NAME:  [MessageHandler(filters.TEXT & ~filters.COMMAND, rg_name)],
            REG_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, rg_phone)],
            REG_ADDR:  [MessageHandler(filters.TEXT & ~filters.COMMAND, rg_addr)],
        },
        fallbacks=[CommandHandler("cancel", rg_cancel)],
    )

    handlers = [
        reg_handler,
        CommandHandler("start",        cmd_start),
        CommandHandler("status",       cmd_status),
        CommandHandler("map",          cmd_map),
        CommandHandler("danger",       cmd_danger),
        CommandHandler("history",      cmd_history),
        CommandHandler("shelters",     cmd_shelters),
        CommandHandler("emergency",    cmd_emergency),
        CommandHandler("rules",        cmd_rules),
        CommandHandler("transport",    cmd_transport),
        CommandHandler("power",        cmd_power),
        CommandHandler("pharmacy",     cmd_pharmacy),
        CommandHandler("schedule",     cmd_schedule),
        CommandHandler("region",       cmd_region),
        CommandHandler("ask_question", cmd_ask),
        CommandHandler("about",        cmd_about),
        CommandHandler("help",         cmd_help),
        CommandHandler("myid",         cmd_myid),
        CommandHandler("admin",        cmd_admin),
        CommandHandler("post",         cmd_post),
        CommandHandler("broadcast",    cmd_broadcast),
        CommandHandler("stats",        cmd_stats),
        CommandHandler("ban",          cmd_ban),
        CommandHandler("unban",        cmd_unban),
        CallbackQueryHandler(btn),
    ]
    for h in handlers:
        tg_app.add_handler(h)

    await tg_app.initialize()

    connector = aiohttp.TCPConnector(limit=20, ttl_dns_cache=300, keepalive_timeout=30)
    async with aiohttp.ClientSession(connector=connector) as session:
        tg_app.bot_data["session"] = session

        await tg_app.bot.set_webhook(url=WEBHOOK_URL, drop_pending_updates=True)
        log.info("🔗 Webhook: %s", WEBHOOK_URL)

        asyncio.create_task(alarm_loop(tg_app.bot, session))

        http = web.Application()
        http.router.add_post("/webhook", wh_handler)
        http.router.add_get("/health",   health)
        http.router.add_get("/",         health)

        runner = web.AppRunner(http)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", PORT).start()

        log.info("🚀 ЯМПІЛЬ АЛЕРТ БОТ v3.0 запущено!")
        log.info("👥 Адмінів: %d | Юзерів: %d | Порт: %d", len(ADMIN_IDS), len(users), PORT)

        await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
