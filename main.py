import os
import threading
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ConversationHandler, filters, ContextTypes
)
from google import genai
from google.genai import types
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
BOT_USERNAME = "UniFeedbackBot"
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

# ================= AI HELPER FUNCTION =================
client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

def ask_ai(user_id, user_prompt, image_bytes=None, file_text=None, voice_bytes=None):
    try:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            return "❌ خطا: متغیر GEMINI_API_KEY در تنظیمات سرور رندر تعریف نشده است!"
            
        client = genai.Client(api_key=api_key)
        
        system_instruction = (
            "تو یک دستیار هوش مصنوعی آموزشی فوق‌العاده هوشمند، همه‌فن‌حریف و مسلط برای دانشجوها هستی. "
            "به سوالات درسی، برنامه‌نویسی و علمی آن‌ها به زبان فارسی روان، دقیق و ساختاریافته پاسخ بده. "
            "اگر کاربر گفت بقیش کو یا ادامه‌اش را بنویس، به تاریخچه چت نگاه کن و دقیقاً ادامه‌ی پاسخ قبلی را بنویس."
        )
        
        if user_id not in chat_histories:
            chat_histories[user_id] = []
            
        current_content = []
        
        if image_bytes:
            img = Image.open(BytesIO(image_bytes))
            current_content.append(img)
            
        if file_text:
            current_content.append(f"[محتوای فایل داکیومنت کاربر]:\n{file_text}")
            
        if voice_bytes:
            current_content.append({
                "mime_type": "audio/ogg",
                "data": bytes(voice_bytes)
            })
            
        if user_prompt:
            current_content.append(user_prompt)
            
        if not current_content:
            return "🤖 گوش به زنگم! می‌توانی متن، عکس، وویس یا فایل برام بفرستی."

        user_summary = user_prompt if user_prompt else "📁 [ارسال فایل/مالتی‌مدیا]"
        chat_histories[user_id].append({'role': 'user', 'parts': [user_summary]})
        
        full_contents = []
        for history_turn in chat_histories[user_id][:-1]:
            full_contents.append(history_turn)
            
        full_contents.append({'role': 'user', 'parts': current_content})
        
        # دریافت یکپارچه پاسخ بدون ایجاد تاخیر صف در سرور
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=full_contents,
            config={'system_instruction': system_instruction}
        )
        
        full_response = response.text if response.text else "⚠️ هوش مصنوعی پاسخی برای این درخواست تولید نکرد."
        
        chat_histories[user_id].append({'role': 'model', 'parts': [full_response]})
        return full_response

    except Exception as e:
        print(f"Gemini Error: {e}")
        return f"⚠️ خطای فنی در اتصال به گوگل:\n`{str(e)}`" 

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

# ================= CENTRAL MESSAGE RECEIVER =================
async def receive_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    user_id = user.id

    if ai_chats.get(user_id):
        waiting_msg = await update.message.reply_text("🤖 در حال پردازش و تحلیل درخواست شما...")
        
        user_text = update.message.text or update.message.caption or ""
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

        # تراز کردن دقیق بخش فراخوانی هوش مصنوعی با بدنه اصلی شرط
        try:
            final_clean_response = ask_ai(user_id, user_text, image_bytes=image_bytes, file_text=file_text, voice_bytes=voice_bytes)
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

    # ================= بخش مدیریت پیام‌های ادمین و چت ناشناس =================
    user_text = update.message.text
    if user_id == ADMIN_ID and user_id in reply_sessions:
        target_id = reply_sessions[user_id]
        user_keyboard = [[InlineKeyboardButton("✉️ پاسخ به ادمین", callback_data="anon_start")], [InlineKeyboardButton("❌ پایان چت", callback_data="end_chat")]]
        try:
            await context.bot.send_message(chat_id=target_id, text=f"📩 **پیام جدید از طرف ادمین:**\n\n{user_text}", reply_markup=InlineKeyboardMarkup(user_keyboard), parse_mode="Markdown")
            await update.message.reply_text(f"✅ پیام شما به کاربر `{target_id}` تحویل داده شد.")
        except:
            await update.message.reply_text("❌ خطا: امکان ارسال پیام به کاربر وجود ندارد.")
        return

    if active_chats.get(user_id):
        username = f"@{user.username}" if user.username else "بدون یوزرنیم"
        admin_keyboard = [[InlineKeyboardButton("✉️ پاسخ به این کاربر", callback_data=f"reply_to:{user_id}")], [InlineKeyboardButton("❌ قطع دسترسی کاربر", callback_data="end_chat")]]
        admin_info = (f"🕵️ **پیام ناشناس جدید**\n👤 **فرستنده:** {user.full_name}\n🆔 `{user_id}` | {username}\n────────────────\n📝 **متن:** {user_text}")
        await context.bot.send_message(chat_id=ADMIN_ID, text=admin_info, reply_markup=InlineKeyboardMarkup(admin_keyboard), parse_mode="Markdown")
        user_status_keyboard = [[InlineKeyboardButton("❌ پایان گفتگو", callback_data="end_chat")]]
        await update.message.reply_text("🚀 پیام شما با موفقیت به ادمین رسید.\nشما می‌توانید پیام‌های بعدی خود را همینجا بفرستید:", reply_markup=InlineKeyboardMarkup(user_status_keyboard))
        return

    await update.message.reply_text("⚠️ لطفاً برای استفاده از امکانات ربات، ابتدا یکی از گزینه‌های منو را در دستور /start انتخاب کنید.")

    # ================= بقیه منطق ادمین و چت ناشناس =================
    user_text = update.message.text
    if user_id == ADMIN_ID and user_id in reply_sessions:
        target_id = reply_sessions[user_id]
        user_keyboard = [[InlineKeyboardButton("✉️ پاسخ به ادمین", callback_data="anon_start")], [InlineKeyboardButton("❌ پایان چت", callback_data="end_chat")]]
        try:
            await context.bot.send_message(chat_id=target_id, text=f"📩 **پیام جدید از طرف ادمین:**\n\n{user_text}", reply_markup=InlineKeyboardMarkup(user_keyboard), parse_mode="Markdown")
            await update.message.reply_text(f"✅ پیام شما به کاربر `{target_id}` تحویل داده شد.")
        except:
            await update.message.reply_text("❌ خطا: امکان ارسال پیام به کاربر وجود ندارد.")
        return

    if active_chats.get(user_id):
        username = f"@{user.username}" if user.username else "بدون یوزرنیم"
        admin_keyboard = [[InlineKeyboardButton("✉️ پاسخ به این کاربر", callback_data=f"reply_to:{user_id}")], [InlineKeyboardButton("❌ قطع دسترسی کاربر", callback_data="end_chat")]]
        admin_info = (f"🕵️ **پیام ناشناس جدید**\n👤 **فرستنده:** {user.full_name}\n🆔 `{user_id}` | {username}\n────────────────\n📝 **متن:** {user_text}")
        await context.bot.send_message(chat_id=ADMIN_ID, text=admin_info, reply_markup=InlineKeyboardMarkup(admin_keyboard), parse_mode="Markdown")
        user_status_keyboard = [[InlineKeyboardButton("❌ پایان گفتگو", callback_data="end_chat")]]
        await update.message.reply_text("🚀 پیام شما با موفقیت به ادمین رسید.\nشما می‌توانید پیام‌های بعدی خود را همینجا بفرستید:", reply_markup=InlineKeyboardMarkup(user_status_keyboard))
        return

    await update.message.reply_text("⚠️ لطفاً برای استفاده از امکانات ربات، ابتدا یکی از گزینه‌های منو را در دستور /start انتخاب کنید.")

# ================= ADMIN CALLBACK HANDLER =================
async def admin_reply_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    target_id = int(update.callback_query.data.split(":")[1])
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
    app.add_handler(MessageHandler((filters.TEXT | filters.PHOTO | filters.VOICE | filters.Document.ALL) & ~filters.COMMAND, receive_msg))

    print("✅ ربات تلگرام با موفقیت آنلاین شد!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
