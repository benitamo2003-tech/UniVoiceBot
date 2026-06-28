import os
import json
import time
import threading
from io import BytesIO

from flask import Flask, request as flask_request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, filters, ContextTypes
)
from google import genai
from PIL import Image
import PyPDF2

# ================================================================
#  CONFIG
# ================================================================
TOKEN            = os.environ.get("BOT_TOKEN", "")
ADMIN_ID         = 7997819976
CHANNEL_ID       = "@UniVoiceHub"
BOT_USERNAME     = "UnifeedbacktecBot"
CHANNEL_LINK     = "https://t.me/UniVoiceHub?direct"
CHANNEL_TAG      = "@UniVoiceHub"
WEBHOOK_URL      = os.environ.get("WEBHOOK_URL", "")   # مثال: https://xxx.onrender.com
GEMINI_API_KEY   = os.environ.get("GEMINI_API_KEY", "")

if GEMINI_API_KEY:
    client = genai.Client(api_key=GEMINI_API_KEY)

# ================================================================
#  STATE FILE  (جلوگیری از پاک شدن بعد restart)
# ================================================================
STATE_FILE = "state.json"

def _load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"reactions": {}, "ai_users": [], "anon_users": [], "reply_sessions": {}}

def _save_state():
    data = {
        "reactions":     {str(k): {"likes": list(v["likes"]), "dislikes": list(v["dislikes"])}
                          for k, v in post_reactions.items()},
        "ai_users":      list(ai_users),
        "anon_users":    list(anon_users),
        "reply_sessions": {str(k): v for k, v in reply_sessions.items()},
    }
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

_s = _load_state()
post_reactions  = {int(k): {"likes": set(v["likes"]), "dislikes": set(v["dislikes"])}
                   for k, v in _s["reactions"].items()}
ai_users        = set(_s["ai_users"])      # user_id هایی که در حالت AI هستن
anon_users      = set(_s["anon_users"])    # user_id هایی که در حالت چت ناشناس هستن
reply_sessions  = {int(k): v for k, v in _s["reply_sessions"].items()}  # admin_id -> target_user_id
chat_histories  = {}                        # user_id -> list (در حافظه، reset قبول داریم)
last_request_time = {}                      # user_id -> timestamp

# ================================================================
#  CONVERSATION STATES
# ================================================================
(ASK_PROF, ASK_COURSE, ASK_TEACHING, ASK_ETHICS, ASK_NOTES,
 ASK_PROJECT, ASK_ATTEND, ASK_MIDTERM, ASK_FINAL, ASK_MATCH,
 ASK_CONTACT, ASK_CONCLUSION, ASK_SEMESTER, ASK_GRADE) = range(14)

FORM_QUESTIONS = [
    ("👨‍🏫 استاد",                        "استاد"),
    ("📚 درس",                           "درس"),
    ("🎓 نوع تدریس",                      "نوع تدریس"),
    ("💬 خصوصیات اخلاقی",                 "خصوصیات اخلاقی"),
    ("📄 جزوه",                          "جزوه"),
    ("🧪 پروژه",                         "پروژه"),
    ("🕒 حضور و غیاب",                    "حضور و غیاب"),
    ("📝 میان‌ترم",                       "میان‌ترم"),
    ("📘 پایان‌ترم",                      "پایان‌ترم"),
    ("📊 تطبیق سوالات با جزوه (از ۵)",    "تطبیق سوالات"),
    ("📞 راه ارتباطی",                    "راه ارتباطی"),
    ("📌 نتیجه‌گیری",                     "نتیجه‌گیری"),
    ("📅 ترم",                           "ترم"),
    ("⭐️ نمره از ۲۰",                    "نمره"),
]

# ================================================================
#  HELPERS
# ================================================================
def reaction_kb(msg_id: int) -> InlineKeyboardMarkup:
    d = post_reactions.get(msg_id, {"likes": set(), "dislikes": set()})
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"👍 {len(d['likes'])}",    callback_data=f"like:{msg_id}"),
        InlineKeyboardButton(f"👎 {len(d['dislikes'])}", callback_data=f"dislike:{msg_id}"),
    ], [
        InlineKeyboardButton("📝 ثبت نظر", url=f"https://t.me/{BOT_USERNAME}?start=form"),
    ]])

def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ انصراف و لغو فرم", callback_data="delete_form"),
    ]])

def build_form_text(data: dict) -> str:
    lines = []
    for title, key in FORM_QUESTIONS:
        lines.append(f"*{title}:*\n{data.get(key, '-')}\n")
    lines += [
        "──────────────",
        "👍 *موافق این نظر هستم*",
        "👎 *مخالف این نظر هستم*",
        "\n⚠️ *مهم: قبل از تصمیم‌گیری بخوانید*",
        f"\n🆔 {CHANNEL_TAG}",
    ]
    return "\n".join(lines)

def exit_all_modes(user_id: int):
    """کاربر رو از همه حالت‌ها خارج می‌کنه."""
    ai_users.discard(user_id)
    anon_users.discard(user_id)
    if user_id in reply_sessions:
        del reply_sessions[user_id]
    _save_state()

# ================================================================
#  /start
# ================================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    exit_all_modes(user_id)
    context.user_data.clear()

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 ثبت نظر درباره استاد",          callback_data="start_form")],
        [InlineKeyboardButton("🤖 دستیار هوش مصنوعی (Gemini)",    callback_data="ai_enter")],
        [InlineKeyboardButton("💬 چت خصوصی",                      url=CHANNEL_LINK)],
        [InlineKeyboardButton("🕵️ چت ناشناس با ادمین",            callback_data="anon_enter")],
    ])
    text = (
        "🎉 سلام! خوش اومدی.\n\n"
        "اینجا می‌تونی تجربه‌ات درباره اساتید رو ناشناس با بقیه دانشجوها به اشتراک بذاری.\n\n"
        "یکی از گزینه‌های زیر رو انتخاب کن 👇"
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=kb)
    else:
        await update.callback_query.answer()
        await update.callback_query.message.edit_text(text, reply_markup=kb)

# ================================================================
#  FORM (ConversationHandler)
# ================================================================
async def cb_start_form(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    exit_all_modes(user_id)
    context.user_data.clear()
    await update.callback_query.answer()
    await update.callback_query.message.reply_text(
        "✨ *شروع ثبت تجربه جدید*\n──────────────\n👨‍🏫 *نام استاد:*\nلطفاً نام استاد را وارد کنید:",
        parse_mode="Markdown", reply_markup=cancel_kb(),
    )
    return ASK_PROF

async def step_course(u, c):
    c.user_data["استاد"] = u.message.text
    await u.message.reply_text("📚 *عنوان درس:*\nنام درس را وارد کنید:", parse_mode="Markdown", reply_markup=cancel_kb())
    return ASK_COURSE

async def step_teaching(u, c):
    c.user_data["درس"] = u.message.text
    await u.message.reply_text("🎓 *شیوه تدریس:*\nنحوه تدریس استاد چطور بود؟", parse_mode="Markdown", reply_markup=cancel_kb())
    return ASK_TEACHING

async def step_ethics(u, c):
    c.user_data["نوع تدریس"] = u.message.text
    await u.message.reply_text("💬 *اخلاق و برخورد:*\nبرخورد استاد با دانشجوها چطور بود؟", parse_mode="Markdown", reply_markup=cancel_kb())
    return ASK_ETHICS

async def step_notes(u, c):
    c.user_data["خصوصیات اخلاقی"] = u.message.text
    await u.message.reply_text("📄 *وضعیت جزوه:*\nآیا استاد جزوه کامل می‌دهد؟", parse_mode="Markdown", reply_markup=cancel_kb())
    return ASK_NOTES

async def step_project(u, c):
    c.user_data["جزوه"] = u.message.text
    await u.message.reply_text("🧪 *پروژه:*\nآیا این درس پروژه داشت؟ نمره‌دهی چطور بود؟", parse_mode="Markdown", reply_markup=cancel_kb())
    return ASK_PROJECT

async def step_attend(u, c):
    c.user_data["پروژه"] = u.message.text
    await u.message.reply_text("🕒 *حضور و غیاب:*\nوضعیت حضور غیاب و حساسیت استاد؟", parse_mode="Markdown", reply_markup=cancel_kb())
    return ASK_ATTEND

async def step_midterm(u, c):
    c.user_data["حضور و غیاب"] = u.message.text
    await u.message.reply_text("📝 *میان‌ترم:*\nامتحان میان‌ترم چطور بود؟", parse_mode="Markdown", reply_markup=cancel_kb())
    return ASK_MIDTERM

async def step_final(u, c):
    c.user_data["میان‌ترم"] = u.message.text
    await u.message.reply_text("📘 *پایان‌ترم:*\nسطح سوالات پایان‌ترم؟", parse_mode="Markdown", reply_markup=cancel_kb())
    return ASK_FINAL

async def step_match(u, c):
    c.user_data["پایان‌ترم"] = u.message.text
    await u.message.reply_text("📊 *تطبیق با جزوه (از ۱ تا ۵):*", parse_mode="Markdown", reply_markup=cancel_kb())
    return ASK_MATCH

async def step_contact(u, c):
    c.user_data["تطبیق سوالات"] = u.message.text
    await u.message.reply_text("📞 *راه ارتباطی:*\nنحوه پاسخگویی استاد؟", parse_mode="Markdown", reply_markup=cancel_kb())
    return ASK_CONTACT

async def step_conclusion(u, c):
    c.user_data["راه ارتباطی"] = u.message.text
    await u.message.reply_text("📌 *نتیجه‌گیری:*\nدر کل این استاد را پیشنهاد می‌کنید؟", parse_mode="Markdown", reply_markup=cancel_kb())
    return ASK_CONCLUSION

async def step_semester(u, c):
    c.user_data["نتیجه‌گیری"] = u.message.text
    await u.message.reply_text("📅 *ترم تحصیلی:*\nچه ترمی با این استاد داشتید؟", parse_mode="Markdown", reply_markup=cancel_kb())
    return ASK_SEMESTER

async def step_grade(u, c):
    c.user_data["ترم"] = u.message.text
    await u.message.reply_text("⭐️ *نمره نهایی (از ۲۰):*", parse_mode="Markdown", reply_markup=cancel_kb())
    return ASK_GRADE

async def step_finish(u, c):
    c.user_data["نمره"] = u.message.text
    summary = build_form_text(c.user_data)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ ارسال نهایی", callback_data="submit_form")],
        [InlineKeyboardButton("🗑 لغو و حذف",  callback_data="delete_form")],
    ])
    await u.message.reply_text(f"🌈 *پیش‌نمایش فرم شما:*\n\n{summary}", reply_markup=kb, parse_mode="Markdown")
    return ConversationHandler.END

async def cb_delete_form(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer("🗑 فرم حذف شد.")
    await update.callback_query.message.edit_text("❌ عملیات لغو شد. برای شروع مجدد /start را بزنید.")
    context.user_data.clear()
    return ConversationHandler.END

async def cb_submit_form(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    summary = build_form_text(context.user_data)
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ تایید انتشار", callback_data=f"admin_accept:{query.from_user.id}"),
        InlineKeyboardButton("❌ رد فرم",       callback_data=f"admin_reject:{query.from_user.id}"),
    ]])
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=f"📥 *فرم جدید دریافت شد:*\n\n{summary}",
        reply_markup=kb, parse_mode="Markdown",
    )
    await query.message.edit_text("📨 فرم شما برای ادمین ارسال شد. پس از بررسی در کانال منتشر می‌شود.")

async def cb_admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, user_id_str = query.data.split(":")
    user_id = int(user_id_str)
    # متن اصلی فرم رو از پیام ادمین می‌خونیم
    form_text = query.message.text.replace("📥 فرم جدید دریافت شد:\n\n", "")

    if action == "admin_accept":
        msg = await context.bot.send_message(chat_id=CHANNEL_ID, text=form_text, parse_mode="Markdown")
        post_reactions[msg.message_id] = {"likes": set(), "dislikes": set()}
        _save_state()
        await msg.edit_reply_markup(reply_markup=reaction_kb(msg.message_id))
        await context.bot.send_message(chat_id=user_id, text="✅ نظر شما تایید و در کانال منتشر شد. ممنون!")
        await query.message.edit_text("✅ با موفقیت در کانال منتشر شد.")
    else:
        await context.bot.send_message(chat_id=user_id, text="❌ فرم شما توسط ادمین تایید نشد.")
        await query.message.edit_text("❌ فرم رد شد.")

# ================================================================
#  REACTION
# ================================================================
async def cb_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, msg_id_str = query.data.split(":")
    msg_id = int(msg_id_str)
    user_id = query.from_user.id
    r = post_reactions.setdefault(msg_id, {"likes": set(), "dislikes": set()})
    if action == "like":
        r["dislikes"].discard(user_id); r["likes"].add(user_id)
    else:
        r["likes"].discard(user_id); r["dislikes"].add(user_id)
    _save_state()
    await query.message.edit_reply_markup(reply_markup=reaction_kb(msg_id))

# ================================================================
#  AI MODE
# ================================================================
async def cb_ai_enter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    exit_all_modes(user_id)
    ai_users.add(user_id)
    _save_state()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🧹 پاک کردن حافظه", callback_data="ai_clear")],
        [InlineKeyboardButton("🔙 بازگشت به منو",   callback_data="back_main")],
    ])
    await query.message.edit_text(
        "🤖 *دستیار هوشمند آموزشی (Gemini)*\n\n"
        "سوال درسی، برنامه‌نویسی یا علمی داری؟ بنویس!\n"
        "می‌تونی عکس، فایل PDF یا متن بفرستی.",
        parse_mode="Markdown", reply_markup=kb,
    )

async def cb_ai_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer("🧹 حافظه پاک شد.")
    chat_histories.pop(update.callback_query.from_user.id, None)
    await update.callback_query.message.reply_text("🔄 تاریخچه پاک شد. گفتگوی جدید شروع شد.")

async def cb_back_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)

def _call_gemini(user_id: int, prompt: str, image_bytes=None, file_text=None, voice_bytes=None) -> str:
    try:
        if not GEMINI_API_KEY:
            return "❌ خطا: GEMINI_API_KEY تنظیم نشده!"
        response = client.models.generate_content(
    model="gemini-2.5-flash",
    contents=parts
            ),
        )
        parts = []
        if image_bytes:
            parts.append({"mime_type": "image/jpeg", "data": bytes(image_bytes)})
        if file_text:
            parts.append(f"[محتوای فایل]:\n{file_text}")
        if voice_bytes:
            parts.append({"mime_type": "audio/ogg", "data": bytes(voice_bytes)})
        if prompt:
            parts.append(prompt)
        if not parts:
            return "گوش به زنگم! متن، عکس یا فایل بفرست."
        resp = model.generate_content(parts)
        return resp.text if resp.text else "⚠️ پاسخی تولید نشد."
    except Exception as e:
        return f"⚠️ خطای فنی: `{e}`"

async def handle_ai_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """فقط برای کاربرانی که در حالت AI هستن صدا زده می‌شه."""
    user_id = update.message.from_user.id
    now = time.time()
    if now - last_request_time.get(user_id, 0) < 12:
        left = int(12 - (now - last_request_time.get(user_id, 0)))
        await update.message.reply_text(f"⚠️ لطفاً {left} ثانیه صبر کن.")
        return
    last_request_time[user_id] = now

    waiting = await update.message.reply_text("🤖 در حال پردازش...")
    prompt      = update.message.text or update.message.caption or ""
    image_bytes = file_text = voice_bytes = None

    try:
        if update.message.photo:
            f = await update.message.photo[-1].get_file()
            image_bytes = await f.download_as_bytearray()
        elif update.message.voice:
            await waiting.edit_text("🎙 در حال شنیدن...")
            f = await update.message.voice.get_file()
            voice_bytes = await f.download_as_bytearray()
        elif update.message.document:
            doc = update.message.document
            name = doc.file_name.lower()
            await waiting.edit_text("📄 در حال خواندن فایل...")
            f = await doc.get_file()
            raw = await f.download_as_bytearray()
            if name.endswith(".pdf"):
                reader = PyPDF2.PdfReader(BytesIO(raw))
                file_text = "".join(p.extract_text() or "" for p in reader.pages)
            elif name.endswith((".txt", ".py", ".cs", ".js", ".html", ".css", ".json")):
                file_text = raw.decode("utf-8", errors="ignore")
            else:
                await waiting.edit_text("❌ فرمت فایل پشتیبانی نمی‌شود.")
                return
    except Exception as e:
        print(f"File error: {e}")

    answer = _call_gemini(user_id, prompt, image_bytes, file_text, voice_bytes)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🧹 پاک کردن حافظه", callback_data="ai_clear")],
        [InlineKeyboardButton("🔙 بازگشت به منو",   callback_data="back_main")],
    ])
    try:
        await waiting.edit_text(answer, parse_mode="Markdown", reply_markup=kb)
    except Exception:
        await waiting.edit_text(answer, reply_markup=kb)

# ================================================================
#  ANON CHAT MODE
# ================================================================
async def cb_anon_enter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    exit_all_modes(user_id)
    anon_users.add(user_id)
    _save_state()
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ پایان چت ناشناس", callback_data="anon_end")]])
    await query.message.reply_text(
        "🕵️ وارد حالت ناشناس شدی.\nهر پیامی بفرستی برای ادمین ارسال می‌شه.",
        reply_markup=kb,
    )

async def handle_anon_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پیام کاربر ناشناس رو به ادمین می‌فرسته."""
    user = update.message.from_user
    user_id = user.id
    username = f"@{user.username}" if user.username else "بدون یوزرنیم"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✉️ پاسخ به این کاربر", callback_data=f"admin_reply_init:{user_id}"),
    ]])
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            f"🕵️ *پیام ناشناس جدید*\n"
            f"👤 {user.full_name} | `{user_id}` | {username}\n"
            f"────────────────\n"
            f"📝 {update.message.text}"
        ),
        reply_markup=kb, parse_mode="Markdown",
    )
    kb2 = InlineKeyboardMarkup([[InlineKeyboardButton("❌ پایان چت ناشناس", callback_data="anon_end")]])
    await update.message.reply_text("✅ پیام شما به ادمین رسید.", reply_markup=kb2)

async def cb_anon_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    anon_users.discard(user_id)
    # اگه ادمین session داشت، target رو هم خبر بده
    target_id = reply_sessions.pop(user_id, None)
    if target_id:
        anon_users.discard(target_id)
        try:
            await context.bot.send_message(chat_id=target_id, text="🔚 گفتگو توسط ادمین پایان یافت.")
        except Exception:
            pass
    _save_state()
    await query.message.edit_text("✅ چت ناشناس پایان یافت. برای شروع مجدد /start بزنید.")

# ================================================================
#  ADMIN REPLY TO ANON USER
# ================================================================
async def cb_admin_reply_init(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ادمین روی دکمه 'پاسخ به این کاربر' کلیک کرده."""
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return
    target_id = int(query.data.split(":")[1])
    # ادمین رو از AI mode خارج کن تا پیام بعدی‌ش به عنوان پاسخ ثبت بشه
    ai_users.discard(ADMIN_ID)
    anon_users.discard(ADMIN_ID)
    reply_sessions[ADMIN_ID] = target_id
    _save_state()
    await query.message.reply_text(
        f"✍️ در حال پاسخ به کاربر `{target_id}` هستید.\n"
        "پیام خود را بنویسید (فقط یک پیام ارسال می‌شود):",
        parse_mode="Markdown",
    )

async def handle_admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """وقتی ادمین در reply_session هست، پیامش رو به کاربر هدف می‌فرسته."""
    target_id = reply_sessions.pop(ADMIN_ID, None)
    _save_state()
    if not target_id:
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✉️ پاسخ به ادمین", callback_data="anon_enter"),
        InlineKeyboardButton("❌ پایان چت",       callback_data="anon_end"),
    ]])
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=f"📩 *پیام از ادمین:*\n\n{update.message.text}",
            reply_markup=kb, parse_mode="Markdown",
        )
        await update.message.reply_text(f"✅ پیام به کاربر `{target_id}` ارسال شد.", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ خطا در ارسال: {e}")

# ================================================================
#  MAIN MESSAGE ROUTER
#  این تابع فقط وقتی کاربر در هیچ حالتی نیست صدا می‌زنه
# ================================================================
async def route_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    user_id = update.message.from_user.id

    # اولویت ۱: ادمین در حال reply به کاربر ناشناس
    if user_id == ADMIN_ID and ADMIN_ID in reply_sessions:
        await handle_admin_reply(update, context)
        return

    # اولویت ۲: حالت AI
    if user_id in ai_users:
        await handle_ai_message(update, context)
        return

    # اولویت ۳: حالت چت ناشناس
    if user_id in anon_users:
        await handle_anon_message(update, context)
        return

    # پیش‌فرض
    await update.message.reply_text(
        "⚠️ برای استفاده از امکانات ربات، ابتدا /start را بزنید."
    )

# ================================================================
#  FLASK + WEBHOOK
# ================================================================
flask_app = Flask(__name__)
_ptb_app: Application = None   # با main پر میشه

@flask_app.route("/")
def home():
    return "Bot is alive!", 200

@flask_app.route(f"/{TOKEN}", methods=["POST"])
async def webhook():
    data = flask_request.get_json(force=True)
    update = Update.de_json(data, _ptb_app.bot)
    await _ptb_app.process_update(update)
    return "ok", 200

# ================================================================
#  MAIN
# ================================================================
def main():
    global _ptb_app

    # ساخت Application
    ptb = Application.builder().token(TOKEN).build()
    _ptb_app = ptb

    # ConversationHandler فرم
    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_start_form, pattern="^start_form$")],
        states={
            ASK_PROF:       [MessageHandler(filters.TEXT & ~filters.COMMAND, step_course)],
            ASK_COURSE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, step_teaching)],
            ASK_TEACHING:   [MessageHandler(filters.TEXT & ~filters.COMMAND, step_ethics)],
            ASK_ETHICS:     [MessageHandler(filters.TEXT & ~filters.COMMAND, step_notes)],
            ASK_NOTES:      [MessageHandler(filters.TEXT & ~filters.COMMAND, step_project)],
            ASK_PROJECT:    [MessageHandler(filters.TEXT & ~filters.COMMAND, step_attend)],
            ASK_ATTEND:     [MessageHandler(filters.TEXT & ~filters.COMMAND, step_midterm)],
            ASK_MIDTERM:    [MessageHandler(filters.TEXT & ~filters.COMMAND, step_final)],
            ASK_FINAL:      [MessageHandler(filters.TEXT & ~filters.COMMAND, step_match)],
            ASK_MATCH:      [MessageHandler(filters.TEXT & ~filters.COMMAND, step_contact)],
            ASK_CONTACT:    [MessageHandler(filters.TEXT & ~filters.COMMAND, step_conclusion)],
            ASK_CONCLUSION: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_semester)],
            ASK_SEMESTER:   [MessageHandler(filters.TEXT & ~filters.COMMAND, step_grade)],
            ASK_GRADE:      [MessageHandler(filters.TEXT & ~filters.COMMAND, step_finish)],
        },
        fallbacks=[CallbackQueryHandler(cb_delete_form, pattern="^delete_form$")],
        # ConversationHandler اول چک میشه، قبل از route_message
        per_message=False,
    )

    # ثبت handler ها (ترتیب مهمه)
    ptb.add_handler(CommandHandler("start", cmd_start))
    ptb.add_handler(conv)   # ← فرم، اول از همه

    # Callback های مختلف
    ptb.add_handler(CallbackQueryHandler(cb_delete_form,       pattern="^delete_form$"))
    ptb.add_handler(CallbackQueryHandler(cb_submit_form,       pattern="^submit_form$"))
    ptb.add_handler(CallbackQueryHandler(cb_admin_action,      pattern="^admin_(accept|reject):\\d+$"))
    ptb.add_handler(CallbackQueryHandler(cb_reaction,          pattern="^(like|dislike):\\d+$"))
    ptb.add_handler(CallbackQueryHandler(cb_ai_enter,          pattern="^ai_enter$"))
    ptb.add_handler(CallbackQueryHandler(cb_ai_clear,          pattern="^ai_clear$"))
    ptb.add_handler(CallbackQueryHandler(cb_back_main,         pattern="^back_main$"))
    ptb.add_handler(CallbackQueryHandler(cb_anon_enter,        pattern="^anon_enter$"))
    ptb.add_handler(CallbackQueryHandler(cb_anon_end,          pattern="^anon_end$"))
    ptb.add_handler(CallbackQueryHandler(cb_admin_reply_init,  pattern="^admin_reply_init:\\d+$"))

    # پیام‌های متنی/رسانه‌ای — router اصلی (آخر از همه)
    ptb.add_handler(MessageHandler(
        (filters.TEXT | filters.PHOTO | filters.VOICE | filters.Document.ALL) & ~filters.COMMAND,
        route_message,
    ))

    if WEBHOOK_URL:
        import asyncio
        async def setup_webhook():
            await ptb.initialize()
            await ptb.bot.set_webhook(url=f"{WEBHOOK_URL}/{TOKEN}")
            print(f"✅ Webhook set: {WEBHOOK_URL}/{TOKEN}")
        asyncio.run(setup_webhook())
        port = int(os.environ.get("PORT", 8080))
        flask_app.run(host="0.0.0.0", port=port)
    else:
        print("✅ Running in polling mode (local)...")
        ptb.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
