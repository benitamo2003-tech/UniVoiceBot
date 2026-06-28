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
import google.generativeai as genai
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
    genai.configure(api_key=GEMINI_API_KEY)

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
        model = genai.GenerativeModel(
            model_name="gemini-2.5-flash",
            system_instruction=(
                "تو یک دستیار هوش مصنوعی آموزشی هستی. "
                "به سوالات درسی، برنامه‌نویسی و علمی به زبان فارسی روان پاسخ بده."
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
        # حالت Webhook (Render)
        import asyncio

        async def setup_webhook():
            await ptb.initialize()
            await ptb.bot.set_webhook(url=f"{WEBHOOK_URL}/{TOKEN}")
            print(f"✅ Webhook set: {WEBHOOK_URL}/{TOKEN}")

        asyncio.get_event_loop().run_until_complete(setup_webhook())
        port = int(os.environ.get("PORT", 8080))
        flask_app.run(host="0.0.0.0", port=port)
    else:
        # حالت Polling (local development)
        print("✅ Running in polling mode (local)...")
        ptb.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()import os
import threading
import time
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ConversationHandler, filters, ContextTypes
)

import google.generativeai as old_genai
from io import BytesIO
from PIL import Image
import PyPDF2

# ================= SERVER FOR RENDER (KEEP ALIVE) =================
app_flask = Flask(__name__)

@app_flask.route("/")
def home():
    return "Bot is Alive!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app_flask.run(host="0.0.0.0", port=port)

# ================= CONFIG =================
TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = 7997819976
CHANNEL_ID = "@UniVoiceHub"
BOT_USERNAME = "UnifeedbacktecBot"
CHANNEL_DIRECT_LINK = "https://t.me/UniVoiceHub?direct"
CHANNEL_TAG = "@UniVoiceHub"

# ================= STATES =================
(ASK_PROF, ASK_COURSE, ASK_TEACHING, ASK_ETHICS, ASK_NOTES,
 ASK_PROJECT, ASK_ATTEND, ASK_MIDTERM, ASK_FINAL, ASK_MATCH,
 ASK_CONTACT, ASK_CONCLUSION, ASK_SEMESTER, ASK_GRADE) = range(14)

# ================= FORM QUESTIONS =================
FORM_QUESTIONS = [
    ("👨‍🏫 استاد", "استاد"), ("📚 درس", "درس"), ("🎓 نوع تدریس", "نوع تدریس"),
    ("💬 خصوصیات اخلاقی", "خصوصیات اخلاقی"), ("📄 جزوه", "جزوه"), ("🧪 پروژه", "پروژه"),
    ("🕒 حضور و غیاب", "حضور و غیاب"), ("📝 میان‌ترم", "میان‌ترم"), ("📘 پایان‌ترم", "پایان‌ترم"),
    ("📊 میزان تطبیق سوالات با جزوه (از 5)", "تطبیق سوالات"), ("📞 راه ارتباطی", "راه ارتباطی"),
    ("📌 نتیجه‌گیری", "نتیجه‌گیری"), ("📅 ترمی که با استاد داشتی", "ترم"), ("⭐️ نمره از ۲۰", "نمره"),
]

post_reactions = {}  # message_id -> {"likes": set(), "dislikes": set()}
reply_sessions = {}
active_chats = {}   # user_id -> True (نشست‌های فعال چت ناشناس)
ai_chats = {}       # user_id -> True (نشست‌های فعال هوش مصنوعی)
chat_histories = {}
user_last_request_time = {}
# =====================================================
# ================= AI HELPER FUNCTION =================
# =====================================================

def ask_ai(user_id, user_prompt, image_bytes=None, file_text=None, voice_bytes=None):
    try:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            return "❌ خطا: متغیر GEMINI_API_KEY در تنظیمات رندر تعریف نشده است!"

        old_genai.configure(api_key=api_key)

        system_instruction = (
            "تو یک دستیار هوش مصنوعی آموزشی فوق‌العاده هوشمند، همه‌فن‌حریف و مسلط برای دانشجوها هستی. "
            "به سوالات درسی، برنامه‌نویسی و علمی آن‌ها به زبان فارسی روان و دقیق پاسخ بده."
        )

        model = old_genai.GenerativeModel(
            model_name='gemini-2.5-flash',
            system_instruction=system_instruction
        )

        if user_id not in chat_histories:
            chat_histories[user_id] = []

        contents = []

        if image_bytes:
            contents.append({'mime_type': 'image/jpeg', 'data': bytes(image_bytes)})

        if file_text:
            contents.append(f"[محتوای فایل داکیومنت کاربر]:\n{file_text}")

        if voice_bytes:
            contents.append({'mime_type': 'audio/ogg', 'data': bytes(voice_bytes)})

        if user_prompt:
            contents.append(user_prompt)

        if not contents:
            return "🤖 گوش به زنگم! می‌توانی متن، عکس، وویس یا فایل برام بفرستی."

        response = model.generate_content(contents)

        if response.text:
            return response.text
        return "⚠️ هوش مصنوعی پاسخی برای این درخواست تولید نکرد."

    except Exception as e:
        print(f"Gemini Engine Error: {e}")
        return f"⚠️ خطای فنی در اتصال به گوگل:\n`{str(e)}`"


# =====================================================
# ================= MESSAGE RECEIVER ===================
# =====================================================

async def receive_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    user_id = user.id
    user_text = update.message.text

    # 🟢 اولویت اول: بررسی اینکه آیا ادمین دارد به یک پیام ناشناس پاسخ می‌دهد
    if user_id == ADMIN_ID and user_id in reply_sessions:
        target_id = reply_sessions[user_id]
        user_keyboard = [[InlineKeyboardButton("✉️ پاسخ به ادمین", callback_data="anon_start")], [InlineKeyboardButton("❌ پایان چت", callback_data="end_chat")]]
        try:
            await context.bot.send_message(chat_id=target_id, text=f"📩 **پیام جدید از طرف ادمین:**\n\n{user_text}", reply_markup=InlineKeyboardMarkup(user_keyboard), parse_mode="Markdown")
            await update.message.reply_text(f"✅ پیام شما به کاربر `{target_id}` تحویل داده شد.")
        except Exception as admin_send_err:
            print(f"Admin send error: {admin_send_err}")
            await update.message.reply_text("❌ خطا: امکان ارسال پیام به کاربر وجود ندارد.")
        return

    # 🔵 اولویت دوم: هندل کردن چت هوش مصنوعی (فقط اگر ادمین در حال پاسخ به کسی نباشد)
    if ai_chats.get(user_id):
        current_time = time.time()
        last_time = user_last_request_time.get(user_id, 0)
        
        if current_time - last_time < 12:  # ۱۲ ثانیه استراحت بین هر پیام
            time_left = int(12 - (current_time - last_time))
            await update.message.reply_text(f"⚠️ رفیق لطفاً اسپم نکن! {time_left} ثانیه دیگه دوباره امتحان کن تا سرور گوگل ارور نداده. 🚦")
            return
            
        user_last_request_time[user_id] = current_time
        waiting_msg = await update.message.reply_text("🤖 در حال پردازش و تحلیل درخواست شما...")
        
        user_text_actual = update.message.text or update.message.caption or ""
        image_bytes = None
        file_text = None
        voice_bytes = None
        
        try:
            if update.message.photo:
                photo_file = await update.message.photo[-1].get_file()
                image_bytes = await photo_file.download_as_bytearray()
                
            elif update.message.voice:
                await waiting_msg.edit_text("🎙 در حال شنیدن صدای شما...")
                voice_file = await update.message.voice.get_file()
                voice_bytes = await voice_file.download_as_bytearray()
                
            elif update.message.document:
                doc = update.message.document
                file_name = doc.file_name.lower()
                
                await waiting_msg.edit_text("📄 در حال خواندن فایل...")
                doc_file = await doc.get_file()
                file_bytes = await doc_file.download_as_bytearray()
                
                if file_name.endswith('.pdf'):
                    pdf_io = BytesIO(file_bytes)
                    reader = PyPDF2.PdfReader(pdf_io)
                    extracted_text = ""
                    for page in reader.pages:
                        extracted_text += page.extract_text() or ""
                    file_text = extracted_text
                elif file_name.endswith(('.txt', '.py', '.cs', '.js', '.html', '.css', '.json')):
                    file_text = file_bytes.decode('utf-8', errors='ignore')
                else:
                    await waiting_msg.edit_text("❌ فرمت فایل پشتیبانی نمی‌شود! فقط فایل‌های PDF، متنی و کدهای برنامه نویسی مجاز هستند.")
                    return

        except Exception as file_err:
            print(f"File Process Error: {file_err}")

        try:
            final_clean_response = ask_ai(user_id, user_text_actual, image_bytes=image_bytes, file_text=file_text, voice_bytes=voice_bytes)
        except Exception as ai_err:
            print(f"AI Call Error: {ai_err}")
            final_clean_response = f"⚠️ خطای فنی در ارتباط با هوش مصنوعی:\n`{str(ai_err)}`"
                        
        keyboard = [
            [InlineKeyboardButton("🧹 پاک کردن حافظه (چت جدید)", callback_data="ai_clear_history")],
            [InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="ai_close")]
        ]
        
        try:
            await waiting_msg.edit_text(text=final_clean_response, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception as markdown_err:
            try:
                await waiting_msg.edit_text(text=final_clean_response, reply_markup=InlineKeyboardMarkup(keyboard))
            except Exception as final_err:
                await update.message.reply_text(text=final_clean_response, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # 🟡 اولویت سوم: بخش ارسال پیام ناشناس توسط کاربر به ادمین
    if active_chats.get(user_id):
        username = f"@{user.username}" if user.username else "بدون یوزرنیم"
        admin_keyboard = [[InlineKeyboardButton("✉️ پاسخ به این کاربر", callback_data=f"reply_to:{user_id}")], [InlineKeyboardButton("❌ قطع دسترسی کاربر", callback_data="end_chat")]]
        admin_info = (f"🕵️ **پیام ناشناس جدید**\n👤 **فرستنده:** {user.full_name}\n🆔 `{user_id}` | {username}\n────────────────\n📝 **متن:** {user_text}")
        await context.bot.send_message(chat_id=ADMIN_ID, text=admin_info, reply_markup=InlineKeyboardMarkup(admin_keyboard), parse_mode="Markdown")
        user_status_keyboard = [[InlineKeyboardButton("❌ پایان گفتگو", callback_data="end_chat")]]
        await update.message.reply_text("🚀 پیام شما با موفقیت به ادمین رسید.\nشما می‌توانید پیام‌های بعدی خود را همینجا بفرستید:", reply_markup=InlineKeyboardMarkup(user_status_keyboard))
        return

    # ۴. پیام پیش‌فرض
    await update.message.reply_text("⚠️ لطفاً برای استفاده از امکانات ربات، ابتدا یکی از گزینه‌های منو را در دستور /start انتخاب کنید.")

# ================= HELPERS =================
def reaction_keyboard(msg_id):
    data = post_reactions.get(msg_id, {"likes": set(), "dislikes": set()})
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"👍 {len(data['likes'])}", callback_data=f"like:{msg_id}"),
            InlineKeyboardButton(f"👎 {len(data['dislikes'])}", callback_data=f"dislike:{msg_id}")
        ],
        [InlineKeyboardButton("📝 ثبت نظر", url=f"https://t.me/{BOT_USERNAME}?start=form")]
    ])

def build_form_text(data):
    lines = []
    for title, key in FORM_QUESTIONS:
        value = data.get(key, "-")
        lines.append(f"*{title}:*\n{value}\n")

    lines.append("──────────────")
    lines.append("👍 *موافق این نظر هستم*")
    lines.append("👎 *مخالف این نظر هستم*")
    lines.append("\n⚠️ *مهم: قبل از تصمیم‌گیری بخوانید*")
    lines.append(f"\n🆔 {CHANNEL_TAG}")
    return "\n".join(lines)

def cancel_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ انصراف و لغو فرم", callback_data="delete_form")]
    ])

# ================= HANDLERS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    user_id = update.effective_user.id
    if user_id in ai_chats: del ai_chats[user_id]
    if user_id in active_chats: del active_chats[user_id]

    keyboard = [
        [InlineKeyboardButton("📝 ثبت نظر درباره استاد", callback_data="start_form")],
        [InlineKeyboardButton("🤖 دستیار هوش مصنوعی (Gemini)", callback_data="ai_menu")],
        [InlineKeyboardButton("💬 چت خصوصی", url=CHANNEL_DIRECT_LINK)],
        [InlineKeyboardButton("🕵️ چت ناشناس با ادمین", callback_data="anon_start")]
    ]
    text = """🎉 سلام به شما رفیق تازه‌وارد! 🎉

خوش اومدی به جایی که می‌تونی با خیال راحت تجربه و نظر خودت درباره اساتید رو با بقیه دانشجوها به اشتراک بذاری! هدف؟ کمک به همه برای انتخاب بهتر ترم‌های بعد 😎

💌 نگران نباش، همه پیام‌ها کاملاً ناشناس ارسال می‌شن، پس راحت باش و هر چی دوست داری بگو.

✨ و یه چیز دیگه: اگه پیشنهادی داری یا دوست داری چیزی به ربات اضافه بشه، حتماً تو دایرکت کانال با من درمیون بذار تا با هم یه تجربه تحصیلی عالی و بی‌دردسر بسازیم!

خب، آماده‌ای شروع کنی؟ 🚀"""
    
    if update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.callback_query.answer()
        await update.callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def start_form(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = "✨ *شروع ثبت تجربه جدید*\n" + "─" * 15 + "\n👨‍🏫 *نام استاد:* \nلطفاً نام استاد را وارد کنید:"
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(msg, parse_mode="Markdown", reply_markup=cancel_markup())
    else:
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=cancel_markup())
    context.user_data.clear()
    return ASK_PROF

async def ask_course(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["استاد"] = update.message.text
    await update.message.reply_text("📚 *عنوان درس:*\nنام درس را وارد کنید:", parse_mode="Markdown", reply_markup=cancel_markup())
    return ASK_COURSE

async def ask_teaching(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["درس"] = update.message.text
    await update.message.reply_text("🎓 *شیوه تدریس:*\nنحوه تدریس استاد چطور بود؟", parse_mode="Markdown", reply_markup=cancel_markup())
    return ASK_TEACHING

async def ask_ethics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["نوع تدریس"] = update.message.text
    await update.message.reply_text("💬 *اخلاق و برخورد:*\nبرخورد استاد با دانشجوها چطور بود؟", parse_mode="Markdown", reply_markup=cancel_markup())
    return ASK_ETHICS

async def ask_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["خصوصیات اخلاقی"] = update.message.text
    await update.message.reply_text("📄 *وضعیت جزوه:*\nآیا استاد جزوه کامل می‌دهد؟", parse_mode="Markdown", reply_markup=cancel_markup())
    return ASK_NOTES

async def ask_project(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["جزوه"] = update.message.text
    await update.message.reply_text("🧪 *پروژه و کار عملی:*\nآیا این درس پروژه داشت؟ نمره‌دهی چطور بود؟", parse_mode="Markdown", reply_markup=cancel_markup())
    return ASK_PROJECT

async def ask_attend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["پروژه"] = update.message.text
    await update.message.reply_text("🕒 *حضور و غیاب:*\nوضعیت حضور غیاب و حساسیت استاد؟", parse_mode="Markdown", reply_markup=cancel_markup())
    return ASK_ATTEND

async def ask_midterm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["حضور و غیاب"] = update.message.text
    await update.message.reply_text("📝 *امتحان میان‌ترم:*\nامتحان میان‌ترم چطور بود؟", parse_mode="Markdown", reply_markup=cancel_markup())
    return ASK_MIDTERM

async def ask_final(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["میان‌ترم"] = update.message.text
    await update.message.reply_text("📘 *امتحان پایان‌ترم:*\nسطح سوالات پایان‌ترم؟", parse_mode="Markdown", reply_markup=cancel_markup())
    return ASK_FINAL

async def ask_match(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["پایان‌ترم"] = update.message.text
    await update.message.reply_text("📊 * (از 1 تا 5) تطبیق با جزوه:*\nتطبیق سوالات با جزوه (از 1 تا 5)؟", parse_mode="Markdown", reply_markup=cancel_markup())
    return ASK_MATCH

async def ask_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["تطبیق سوالات"] = update.message.text
    await update.message.reply_text("📞 *راه ارتباطی:*\nنحوه پاسخگویی و ارتباط با استاد؟", parse_mode="Markdown", reply_markup=cancel_markup())
    return ASK_CONTACT

async def ask_conclusion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["راه ارتباطی"] = update.message.text
    await update.message.reply_text("📌 *نتیجه‌گیری:*\nدر کل این استاد را پیشنهاد می‌کنید؟", parse_mode="Markdown", reply_markup=cancel_markup())
    return ASK_CONCLUSION

async def ask_semester(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["نتیجه‌گیری"] = update.message.text
    await update.message.reply_text("📅 *ترم تحصیلی:*\nچه ترمی با این استاد داشتید؟", parse_mode="Markdown", reply_markup=cancel_markup())
    return ASK_SEMESTER

async def ask_grade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["ترم"] = update.message.text
    await update.message.reply_text("⭐️ *نمره نهایی:*\nنمره‌ای که از این درس گرفتید (از 20)؟", parse_mode="Markdown", reply_markup=cancel_markup())
    return ASK_GRADE

async def finish_form(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["نمره"] = update.message.text
    summary = build_form_text(context.user_data)
    keyboard = [[InlineKeyboardButton("✅ ارسال نهایی", callback_data="submit_form")],
                [InlineKeyboardButton("🗑 لغو و حذف", callback_data="delete_form")]]
    await update.message.reply_text(f"🌈 *پیش‌نمایش فرم شما:*\n\n{summary}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return ConversationHandler.END

async def delete_form(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("🗑 فرم حذف شد.")
    await query.message.edit_text("❌ عملیات ثبت نظر لغو شد. برای شروع مجدد /start را بزنید.")
    context.user_data.clear()
    return ConversationHandler.END

# ================= SUBMIT & ADMIN =================
async def submit_form(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    summary = build_form_text(context.user_data)
    keyboard = [
        [InlineKeyboardButton("✅ تایید انتشار", callback_data=f"admin_accept:{query.from_user.id}"),
         InlineKeyboardButton("❌ رد فرم", callback_data=f"admin_reject:{query.from_user.id}")]
    ]
    await context.bot.send_message(chat_id=ADMIN_ID, text=f"📥 فرم جدید دریافت شد:\n\n{summary}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    await query.message.edit_text("📨 فرم شما برای ادمین ارسال شد. پس از بررسی در کانال منتشر می‌شود.")

async def admin_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split(":")
    action = data[0]
    user_id = int(data[1])

    if action == "admin_accept":
        msg = await context.bot.send_message(chat_id=CHANNEL_ID, text=query.message.text.replace("📥 فرم جدید دریافت شد:\n\n", ""), parse_mode="Markdown")
        post_reactions[msg.message_id] = {"likes": set(), "dislikes": set()}
        await msg.edit_reply_markup(reply_markup=reaction_keyboard(msg.message_id))
        await context.bot.send_message(chat_id=user_id, text="✅ نظر شما تایید و در کانال منتشر شد. ممنون از مشارکت شما!")
        await query.message.edit_text("✅ با موفقیت در کانال منتشر شد.")
    else:
        await context.bot.send_message(chat_id=user_id, text="❌ متاسفانه فرم شما توسط ادمین تایید نشد.")
        await query.message.edit_text("❌ فرم رد شد.")

# ================= REACTION SYSTEM =================
async def handle_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, msg_id = query.data.split(":")
    msg_id = int(msg_id)
    user_id = query.from_user.id
    
    res = post_reactions.setdefault(msg_id, {"likes": set(), "dislikes": set()})
    if action == "like":
        res["dislikes"].discard(user_id)
        res["likes"].add(user_id)
    else:
        res["likes"].discard(user_id)
        res["dislikes"].add(user_id)
    await query.message.edit_reply_markup(reply_markup=reaction_keyboard(msg_id))

# ================= AI HANDLERS =================
async def ai_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    user_id = update.callback_query.from_user.id
    ai_chats[user_id] = True  # فعال کردن نشست هوش مصنوعی
    if user_id in active_chats: del active_chats[user_id] 
    
    keyboard = [
        [InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="ai_close")]
    ]
    guide_text = (
        "🤖 *به دستیار هوشمند آموزشی (Gemini) خوش آمدید!* 🤖\n\n"
        "من اینجام تا توی تمام کارهای درسی، دانشگاهی و برنامه‌نویسی کمکت کنم. "
        "هر سوال علمی، حل تمرین، خلاصه‌سازی جزوه یا رفع اشکال کد داری، همین الان برام بفرست!\n\n"
        "✍️ *لطفاً سوال خودت را در کادر زیر بنویس و ارسال کن:*"
    )
    await update.callback_query.message.edit_text(guide_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def ai_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    if user_id in ai_chats: 
        del ai_chats[user_id]
    await start(update, context)

async def ai_clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("🧹 حافظه گفتگو پاک شد.")
    user_id = query.from_user.id
    if user_id in chat_histories:
        chat_histories[user_id] = []
    await query.message.reply_text("🔄 تاریخچه چت شما با هوش مصنوعی کاملاً پاک شد و گفتگو جدید شروع شد.")

# ================= ANON CHAT HANDLERS =================
async def anon_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    user_id = update.callback_query.from_user.id
    active_chats[user_id] = True  
    if user_id in ai_chats: del ai_chats[user_id] 
    
    keyboard = [[InlineKeyboardButton("❌ پایان چت ناشناس", callback_data="end_chat")]]
    await update.callback_query.message.reply_text(
        "🕵️ وارد حالت ناشناس شدی.\nهر پیامی بفرستی برای ادمین ارسال می‌شه. برای خروج دکمه زیر رو بزن:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def end_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    if user_id in active_chats:
        del active_chats[user_id]
    
    if user_id == ADMIN_ID and user_id in reply_sessions:
        target_id = reply_sessions[user_id]
        if target_id in active_chats: del active_chats[target_id]
        await context.bot.send_message(chat_id=target_id, text="🔚 ادمین به این گفتگو پایان داد.")
        del reply_sessions[user_id]

    await query.message.edit_text("✅ چت پایان یافت. برای شروع مجدد /start را بزنید.")

# ================= ADMIN CALLBACK HANDLER =================
# ================= ADMIN CALLBACK HANDLER =================
async def admin_reply_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    target_id = int(update.callback_query.data.split(":")[1])
    
    # محکم‌کاری: اگر هوش مصنوعی ادمین روشن است، آن را خاموش کن تا پاسخ به کاربر ارسال شود
    if ADMIN_ID in ai_chats: 
        del ai_chats[ADMIN_ID]
        
    reply_sessions[ADMIN_ID] = target_id
    await update.callback_query.message.reply_text(f"✍️ در حال پاسخ به `{target_id}` هستید. پیام خود را بفرستید:")

# ================= MAIN FUNCTION =================
def main():
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    print("✅ سرور Flask در پس‌زمینه فعال شد.")

    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_form, pattern="^start_form$")],
        states={
            ASK_PROF: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_course)],
            ASK_COURSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_teaching)],
            ASK_TEACHING: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_ethics)],
            ASK_ETHICS: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_notes)],
            ASK_NOTES: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_project)],
            ASK_PROJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_attend)],
            ASK_ATTEND: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_midterm)],
            ASK_MIDTERM: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_final)],
            ASK_FINAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_match)],
            ASK_MATCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_contact)],
            ASK_CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_conclusion)],
            ASK_CONCLUSION: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_semester)],
            ASK_SEMESTER: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_grade)],
            ASK_GRADE: [MessageHandler(filters.TEXT & ~filters.COMMAND, finish_form)],
        },
        fallbacks=[CallbackQueryHandler(delete_form, pattern="^delete_form$")]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(submit_form, pattern="^submit_form$"))
    app.add_handler(CallbackQueryHandler(admin_actions, pattern="^admin_(accept|reject):"))
    app.add_handler(CallbackQueryHandler(handle_reaction, pattern="^(like|dislike):"))
    app.add_handler(CallbackQueryHandler(ai_menu, pattern="^ai_menu$"))
    app.add_handler(CallbackQueryHandler(ai_close, pattern="^ai_close$"))
    app.add_handler(CallbackQueryHandler(ai_clear_history, pattern="^ai_clear_history$"))
    app.add_handler(CallbackQueryHandler(anon_start, pattern="^anon_start$"))
    app.add_handler(CallbackQueryHandler(admin_reply_start, pattern="^reply_to:"))
    app.add_handler(CallbackQueryHandler(end_chat, pattern="^end_chat$"))
    
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.PHOTO | filters.VOICE | filters.Document.ALL) & 
        ~filters.COMMAND, 
        receive_msg
    ))

    print("✅ ربات تلگرام با موفقیت آنلاین شد!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
