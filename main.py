from telegram import ReplyKeyboardMarkup, KeyboardButton, Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
import time as time_module
import os
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from flask_socketio import SocketIO
import threading
import re
import db
import random

from db import (
    add_user,
    update_user_name,
    user_exists,
    has_valid_phone,
    get_user,
    get_user_name,
    set_referral,
    get_main_balance,
    get_play_balance,
    update_main_balance,
    update_play_balance,
    deduct_bet_smart,
    add_transaction,
    get_last_5_transactions,
    get_all_transactions,
    get_total_deposits,
    get_referral_count,
    is_user_agent,
    check_and_upgrade_agent,
    get_depositing_referrals_count,
    get_total_referral_deposits,
    get_user_by_phone,
    transaction_exists,
    get_user_language,
    set_user_language,
    add_game_session,
    complete_game_session,
    get_game_history,
    get_games_played_count,
    get_games_won_count,
    get_total_won,
    get_total_bet,
    get_last_played,
    get_top_by_deposit,
    get_top_by_invitations,
    get_top_by_games,
    get_top_by_wins,
    get_user_rank,
    get_user_phone,
    get_dashboard_stats,
    get_all_deposits,
    approve_deposit,
    reject_deposit,
    get_all_withdrawals,
    approve_withdrawal,
    reject_withdrawal,
    get_all_users_with_stats,
    ban_user,
    unban_user,
    mark_vip,
    get_admin_game_history,
    get_admin_reports,
    freeze_user,
    unfreeze_user,
)

# --------------------------
# CONFIG
# --------------------------
TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError(
        "BOT_TOKEN environment variable is not set. Set it in your Render "
        "service's Environment settings (or your shell before running "
        "locally) - the bot will not start without it."
    )
BOT_USERNAME = "adwabingiobot"
def _env_admin_ids():
    raw = os.environ.get("ADMIN_IDS", "7627811244,1119881250,2073850894")
    ids = [int(p.strip()) for p in raw.split(",") if p.strip().isdigit()]
    return ids or [7627811244, 1119881250, 2073850894]
ADMIN_IDS = _env_admin_ids()
MINI_APP_URL = os.environ.get("WEBAPP_URL", "https://sebez733-png.github.io/bingio-mini-app/")

ADMIN_CREDENTIALS = {
    'superadmin': {'password': os.environ.get("ADMIN_PASSWORD", "admin123"), 'role': 'super'},
    'admin1':     {'password': os.environ.get("ADMIN2_PASSWORD", "pass123"),  'role': 'regular'},
}

# --------------------------
# TELEBIRR SMS VERIFICATION
# --------------------------
MERCHANT_PHONE = os.environ.get("MERCHANT_PHONE", "0998480054")

def get_merchant_phone_partials():
    p = MERCHANT_PHONE
    local_partial = p[:4] + "****" + p[-2:]
    intl = "251" + p[1:]
    intl_partial = intl[:4] + "****" + intl[-4:]
    return [local_partial, intl_partial]

def _is_transaction_used(transaction_id: str) -> bool:
    from db import db as mongo_db
    return mongo_db["telebirr_transactions"].find_one({"transaction_id": transaction_id}) is not None

def _mark_transaction_used(transaction_id: str, user_id: int, amount: float):
    from db import db as mongo_db
    from datetime import datetime
    mongo_db["telebirr_transactions"].update_one(
        {"transaction_id": transaction_id},
        {"$setOnInsert": {"transaction_id": transaction_id, "user_id": user_id, "amount": amount, "created_at": datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}},
        upsert=True
    )

def verify_telebirr_sms(sms_text: str, expected_amount: int) -> dict:
    sms_text = sms_text.strip()
    if "transferred ETB" not in sms_text:
        return {
            'valid': False,
            'reason': (
                "❌ SMS format not recognized.\n\n"
                "Please paste the *exact* SMS you received from Telebirr after sending money.\n\n"
                "Example:\n"
                "_Dear Habtamu You have transferred ETB 100.00 to ..._"
            )
        }
    amount_match = re.search(r'transferred ETB\s*([\d,]+\.?\d*)', sms_text)
    if not amount_match:
        return {'valid': False, 'reason': "❌ Could not read amount from SMS. Please paste the full SMS."}
    amount = float(amount_match.group(1).replace(',', ''))
    txn_match = re.search(r'transaction number is\s*([A-Z0-9]+)', sms_text)
    if not txn_match:
        return {'valid': False, 'reason': "❌ Could not find transaction number in SMS. Please paste the full SMS."}
    transaction_id = txn_match.group(1).strip()
    phone_match = re.search(r'\((\d{4}\*+\d{2,4})\)', sms_text)
    if phone_match:
        receiver_partial = phone_match.group(1)
        allowed_partials = get_merchant_phone_partials()
        if receiver_partial not in allowed_partials:
            return {
                'valid': False,
                'reason': (
                    f"❌ Wrong recipient!\n\n"
                    f"Money was not sent to our account.\n"
                    f"Please send to: `{MERCHANT_PHONE}`"
                )
            }
    date_match = re.search(r'on\s*(\d{2}/\d{2}/\d{4})\s*(\d{2}:\d{2}:\d{2})', sms_text)
    date_str = date_match.group(1) if date_match else ''
    time_str = date_match.group(2) if date_match else ''
    if abs(amount - expected_amount) > 1:
        return {
            'valid': False,
            'reason': (
                f"❌ Amount mismatch!\n\n"
                f"You said you'd send *{expected_amount} ETB* "
                f"but SMS shows *{amount:.2f} ETB*.\n\n"
                f"Please make sure you send the exact amount."
            )
        }
    if _is_transaction_used(transaction_id):
        return {
            'valid': False,
            'reason': (
                f"❌ Transaction already used!\n\n"
                f"Transaction `{transaction_id}` was already submitted.\n"
                f"Each SMS can only be used once."
            )
        }
    return {
        'valid': True,
        'reason': 'OK',
        'transaction_id': transaction_id,
        'amount': amount,
        'date': date_str,
        'time': time_str,
    }

# --------------------------
# STATE & COUNTERS
# --------------------------
user_state = {}
request_counter = 0
withdraw_requests = {}

# --------------------------
# MULTIPLAYER ENGINE (Step 2 & 3)
# --------------------------
from backend.multiplayer.room_manager import RoomManager, Room
from backend.multiplayer.game_engine import GameEngine
from backend.multiplayer.socket_manager import SocketManager

room_manager = RoomManager()     # unlimited dynamic rooms
game_engine = GameEngine(room_manager)


def legacy_state_for_stake(stake):
    """Legacy /api/game_state shape resolved from the current open room."""
    room = room_manager.get_open_room(stake)
    if room is None:
        room = room_manager.create_room(stake)
    return {
        'room': stake,
        'game_running': room.status == Room.RUNNING,
        'game_id': room.game_id,
        'time_left': room.time_left(),
        'total_players': room.total_cards(),
        'called_numbers': list(room.called),
        'current_number': room.current_number,
        'call_count': len(room.called),
        'players': room.player_count(),
        'prize_pool': room.prize_pool(),
    }

# --------------------------
# TRANSLATION DICTIONARY
# --------------------------
TEXTS = {
    'select_language': {'am': "👇 ቋንቋ ይምረጡ / Please select your language", 'en': "👇 Please select your language"},
    'welcome_new': {
        'am': "🎉 እንኳን ወደ አድዋ Bingo በደህና መጡ!\n\n1️⃣ ከታች ያለውን \"📱 ስልክ ቁጥር ያጋሩ\" ይጫኑ\n2️⃣ ስልክ ቁጥርዎን ያረጋግጡ\n3️⃣ ከዚያ በኋላ መጫወት ይጀምሩ! 🚀\n\n👇 ለመጀመር ስልክ ቁጥርዎን ያጋሩ",
        'en': "🎉 Welcome to our Adwa Bingo Game!\n\n1️⃣ Click the button below to share your phone number\n2️⃣ Verify your number\n3️⃣ Start playing! 🚀\n\n👇 Share your phone number to begin:"
    },
    'share_phone_btn': {'am': "📱 ስልክ ቁጥር ያጋሩ", 'en': "📱 Share Phone Number"},
    'welcome_back': {'am': "👋 እንኳን ደህና መጡ!", 'en': "👋 Welcome back!"},
    'already_registered': {
        'am': "⚠️ እርስዎ ቀድሞ ተመዝግበዋል!\n\n📱 ስልክ: {phone}\n\n💰 Main Wallet: {main} ETB\n🎮 Play Wallet: {play} ETB\n👥 Referrals: {ref_count}\n\n👇 Choose an option below:",
        'en': "⚠️ You are already registered!\n\n📱 Phone: {phone}\n\n💰 Main Wallet: {main} ETB\n🎮 Play Wallet: {play} ETB\n👥 Referrals: {ref_count}\n\n👇 Choose an option below:"
    },
    'register_success': {
        'am': "🎉 እንኳን ወደ አድዋ Bingo ቤተሰብ በደህና መጡ!\n\n✅ ምዝገባዎ በተሳካ ሁኔታ ተጠናቋል!\n\n📱 ስልክ ቁጥር: {phone}\n\n🎁 የመነሻ ስጦታ: 1000 ETB\n\n💰 Main Wallet: {main} ETB\n🎮 Play Wallet: {play} ETB\n\n🎯 አሁን መጫወት ለመጀመር ከታች ያለውን ቁልፍ ይጫኑ!\n🍀 መልካም እድል!",
        'en': "🎉 Welcome to the Adwa Bingo Family!\n\n✅ Registration successful!\n\n📱 Phone: {phone}\n\n🎁 Start Bonus: 1000 ETB\n\n💰 Main Wallet: {main} ETB\n🎮 Play Wallet: {play} ETB\n\n🎯 Click the menu below to start playing!\n🍀 Good luck!"
    },
    'deposit_prompt': {'am': "💳 ምን ያህል ማስገባት ይፈልጋሉ?\n(Enter amount)\n\nMin / ዝቅተኛ: 10 ብር / Birr", 'en': "💳 How much would you like to deposit?\n(Enter amount)\n\nMin: 10 Birr"},
    'withdraw_prompt': {
        'am': "🐝 ማውጣት የሚፈልጉትን መጠን ይፃፉ (ETB):\n\n🎮 Play Wallet: {play_bal} ETB\n💰 Main Wallet: {main_bal} ETB\n\nMin / ዝቅተኛ: 100 ብር",
        'en': "🐝 Enter withdrawal amount (ETB):\n\n🎮 Play Wallet: {play_bal} ETB\n💰 Main Wallet: {main_bal} ETB\n\nMin: 100 Birr"
    },
    'withdraw_locked': {
        'am': "❌ ማውጣት አይችሉም!\n\n⚠️ ገንዘብ ለማውጣት 50 ብር ማስገባት አለብዎት።",
        'en': "❌ Withdrawal locked!\n\n⚠️ You must deposit at least 50 ETB in total to unlock withdrawals."
    },
    'balance_msg': {'am': "💰 WALLET BALANCE\n\n💰 Main Wallet: {main} ETB\n🎮 Play Wallet: {play} ETB", 'en': "💰 WALLET BALANCE\n\n💰 Main Wallet: {main} ETB\n🎮 Play Wallet: {play} ETB"},
    'deposit_success': {
        'am': "✅ Deposit Successful\n\n💰 Method: {method}\n💰 Sent: {amount}\n🎁 Bonus: {bonus}\n📈 Total Added: {total}\n💰 New Balance: {new_balance} ETB",
        'en': "✅ Deposit Successful\n\n💰 Method: {method}\n💰 Sent: {amount}\n🎁 Bonus: {bonus}\n📈 Total Added: {total}\n💰 New Balance: {new_balance} ETB"
    },
    'lang_changed': {'am': "✅ ቋንቋ ወደ አማርኛ ተቀይሯል!", 'en': "✅ Language changed to English!"}
}

def t(key, lang='am', **kwargs):
    text = TEXTS.get(key, {}).get(lang, TEXTS.get(key, {}).get('am', key))
    if kwargs: text = text.format(**kwargs)
    return text

# --------------------------
# HELPERS
# --------------------------
def normalize_phone(phone):
    phone = phone.replace(" ", "").replace("+", "").replace("-", "").replace("(", "").replace(")", "")
    if phone.startswith("251"): phone = "0" + phone[3:]
    if not phone.startswith("0") and len(phone) == 9: phone = "0" + phone
    return phone

def get_main_menu(lang='am'):
    if lang == 'en':
        return ReplyKeyboardMarkup([["🎮 Open Game"], ["💳 Deposit", "💰 Balance"], ["🐝 Withdraw", "📜 History"], ["👤 Profile", "🏢 Support"], ["🎁 Invite Friends", "🤖 Agent Panel"], ["🔄 Transfer", "ℹ️ Info"]], resize_keyboard=True)
    else:
        return ReplyKeyboardMarkup([["🎮 Open Game / ይጫወቱ"], ["💳 Deposit / ያስገቡ", "💰 Balance / ሂሳብ"], ["🐝 Withdraw / ያውጡ", "📜 History / ታሪክ"], ["👤 Profile / መገለጫ", "🏢 Support / ድጋፍ"], ["🎁 Invite Friends / ጓደኛ ይጋብዙ", "🤖 Agent Panel"], ["🔄 Transfer / ይላኩ", "ℹ️ Info / መረጃ"]], resize_keyboard=True)

def get_inline_menu(lang='am'):
    if lang == 'en':
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🎮 Open Game", web_app=WebAppInfo(url=MINI_APP_URL))],
            [InlineKeyboardButton("💳 Deposit", callback_data="menu_deposit"), InlineKeyboardButton("💰 Balance", callback_data="menu_balance")],
            [InlineKeyboardButton("🐝 Withdraw", callback_data="menu_withdraw"), InlineKeyboardButton("📜 History", callback_data="menu_history")],
            [InlineKeyboardButton("👤 Profile", callback_data="menu_profile"), InlineKeyboardButton("🏢 Support", callback_data="menu_support")],
            [InlineKeyboardButton("🎁 Invite Friends", callback_data="menu_invite"), InlineKeyboardButton("🤖 Agent Panel", callback_data="menu_agent")],
            [InlineKeyboardButton("🔄 Transfer", callback_data="menu_transfer"), InlineKeyboardButton("ℹ️ Info", callback_data="menu_info")]
        ])
    else:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🎮 Open Game / ይጫወቱ", web_app=WebAppInfo(url=MINI_APP_URL))],
            [InlineKeyboardButton("💳 Deposit / ያስገቡ", callback_data="menu_deposit"), InlineKeyboardButton("💰 Balance / ሂሳብ", callback_data="menu_balance")],
            [InlineKeyboardButton("🐝 Withdraw / ያውጡ", callback_data="menu_withdraw"), InlineKeyboardButton("📜 History / ታሪክ", callback_data="menu_history")],
            [InlineKeyboardButton("👤 Profile / መገለጫ", callback_data="menu_profile"), InlineKeyboardButton("🏢 Support / ድጋፍ", callback_data="menu_support")],
            [InlineKeyboardButton("🎁 Invite Friends / ጓደኛ ይጋብዙ", callback_data="menu_invite"), InlineKeyboardButton("🤖 Agent Panel", callback_data="menu_agent")],
            [InlineKeyboardButton("🔄 Transfer / ይላኩ", callback_data="menu_transfer"), InlineKeyboardButton("ℹ️ Info / መረጃ", callback_data="menu_info")]
        ])

def get_register_keyboard(lang='am'):
    button_text = t('share_phone_btn', lang)
    button = KeyboardButton(button_text, request_contact=True)
    return ReplyKeyboardMarkup([[button]], resize_keyboard=True, one_time_keyboard=True)

# --------------------------
# START
# --------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    first_name = update.effective_user.first_name or ''
    ref_id = context.args[0] if context.args else None
    context.user_data["ref_by"] = ref_id

    if has_valid_phone(user_id):
        lang = get_user_language(user_id)
        update_user_name(user_id, first_name)
        menu = get_main_menu(lang)
        await update.message.reply_text(t('welcome_back', lang), reply_markup=menu)
        return

    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🇪🇹 አማርኛ", callback_data="lang_am"), InlineKeyboardButton("🇸🇸 English", callback_data="lang_en")]])
    await update.message.reply_text(t('select_language'), reply_markup=keyboard)

# --------------------------
# CONTACT REGISTER
# --------------------------
async def get_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    first_name = update.effective_user.first_name or ''
    phone = normalize_phone(update.message.contact.phone_number)
    lang = context.user_data.get("lang", 'am')

    if has_valid_phone(user_id):
        user = get_user(user_id)
        existing_phone = user[1] if user else ''
        lang = get_user_language(user_id)
        main = get_main_balance(user_id)
        play = get_play_balance(user_id)
        ref_count = get_referral_count(user_id)
        text = t('already_registered', lang, phone=existing_phone, main=main, play=play, ref_count=ref_count)
        await update.message.reply_text(text, reply_markup=get_inline_menu(lang))
        await update.message.reply_text("⬇️ Menu:", reply_markup=get_main_menu(lang))
        return

    ref_by = context.user_data.get("ref_by")
    if user_exists(user_id):
        # Ghost user fix
        try:
            from db import db as mongo_db
            mongo_db["users"].update_one({"user_id": user_id}, {"$set": {"phone": phone, "first_name": first_name, "language": lang}})
        except Exception as e:
            print(f"❌ Phone update error for ghost user {user_id}: {e}")
            await update.message.reply_text("❌ Registration failed. Please try again.")
            return
    else:
        try:
            add_user(user_id, phone, first_name)
            set_user_language(user_id, lang)
        except Exception as e:
            print(f"❌ Registration error for new user {user_id}: {e}")
            await update.message.reply_text("❌ Registration failed. Please try again.\nContact support: @one_day_82")
            return

    if ref_by:
        try:
            set_referral(user_id, ref_by)
        except Exception as e:
            print(f"❌ Referral error for user {user_id}: {e}")

    main = get_main_balance(user_id)
    play = get_play_balance(user_id)
    text = t('register_success', lang, phone=phone, main=main, play=play)
    await update.message.reply_text(text, reply_markup=get_inline_menu(lang))
    await update.message.reply_text("⬇️ Menu:", reply_markup=get_main_menu(lang))

# --------------------------
# TEXT HANDLER
# --------------------------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE, custom_text=None):
    global request_counter
    user_id = update.effective_user.id
    text = custom_text if custom_text is not None else update.message.text
    first_name = update.effective_user.first_name or ''

    if not has_valid_phone(user_id):
        lang = context.user_data.get("lang", 'am')
        keyboard = get_register_keyboard(lang)
        await update.message.reply_text("⚠️ You must register first!\n\n" + t('welcome_new', lang), reply_markup=keyboard)
        return

    if first_name: update_user_name(user_id, first_name)
    lang = get_user_language(user_id)

    main_menu_buttons_am = ["🎮 Open Game / ይጫወቱ", "💳 Deposit / ያስገቡ", "💰 Balance / ሂሳብ", "🐝 Withdraw / ያውጡ", "📜 History / ታሪክ", "👤 Profile / መገለጫ", "🏢 Support / ድጋፍ", "🎁 Invite Friends / ጓደኛ ይጋብዙ", "🤖 Agent Panel", "🔄 Transfer / ይላኩ", "ℹ️ Info / መረጃ"]
    main_menu_buttons_en = ["🎮 Open Game", "💳 Deposit", "💰 Balance", "🐝 Withdraw", "📜 History", "👤 Profile", "🏢 Support", "🎁 Invite Friends", "🤖 Agent Panel", "🔄 Transfer", "ℹ️ Info"]

    if text in main_menu_buttons_am or text in main_menu_buttons_en:
        for k in [user_id, f"{user_id}_amount", f"{user_id}_withdraw_amount", f"{user_id}_method", f"{user_id}_transfer_wallet", f"{user_id}_transfer_target"]:
            user_state.pop(k, None)

    if text in ["🎮 Open Game / ይጫወቱ", "🎮 Open Game"]:
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🎲 Play Bingo Now", web_app=WebAppInfo(url=MINI_APP_URL))]])
        game_msg = "🎮 Tap the button below to open the Bingo Game:" if lang == 'en' else "🎮 የቢንጎ ጨዋታውን ለመክፈት ከታች ያለውን ቁልፍ ይጫኑ:"
        await update.message.reply_text(game_msg, reply_markup=keyboard)
        return

    if text in ["💰 Balance / ሂሳብ", "💰 Balance"]:
        main = get_main_balance(user_id); play = get_play_balance(user_id)
        await update.message.reply_text(t('balance_msg', lang, main=main, play=play))
        return

    if text in ["🏢 Support / ድጋፍ", "🏢 Support"]:
        support_msg = "☎️ Support\n\nFor any comments or questions, contact support:\n@thelastking12312345678\n@Silencedoeir\n@one_day_82"
        await update.message.reply_text(support_msg)
        return

    if text in ["📜 History / ታሪክ", "📜 History"]:
        history = get_last_5_transactions(user_id)
        if not history:
            await update.message.reply_text("📜 No transactions yet.")
            return
        msg = "📜 LAST 5 TRANSACTIONS\n\n"
        for tx in history:
            tx_type, amount, time_str = tx
            icon = "🟢 Deposit" if tx_type == "deposit" else "🔴 Withdraw"
            msg += f"{icon}\n💰 Amount: {amount} ETB\n⏰ Date: {time_str.split('.')[0]}\n\n"
        await update.message.reply_text(msg)
        return

    if text in ["👤 Profile / መገለጫ", "👤 Profile"]:
        user = get_user(user_id)
        if not user: await update.message.reply_text("❌ User not found"); return
        link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
        ref_count = get_referral_count(user_id); played = get_games_played_count(user_id); won = get_games_won_count(user_id); total_won = get_total_won(user_id)
        profile_msg = f"👤 PROFILE\n\n🆔 ID: {user[0]}\n📱 Phone: {user[1]}\n\n💰 Main Wallet: {user[2]} ETB\n🎮 Play Wallet: {user[3]} ETB\n\n🎯 Games Played: {played}\n🏆 Games Won: {won}\n💵 Total Won: {total_won} ETB\n\n👥 Referrals: {ref_count}\n🎯 Invited By: {user[4] if len(user) > 4 and user[4] else 'No inviter'}\n\n🎁 Invite Link:\n{link}"
        await update.message.reply_text(profile_msg)
        return

    if text in ["🎁 Invite Friends / ጓደኛ ይጋብዙ", "🎁 Invite Friends"]:
        link = f"https://t.me/{BOT_USERNAME}?start={user_id}"; ref_count = get_referral_count(user_id)
        invite_msg = f"🎁 Invite Friends System\n\n👥 Your Invites: {ref_count}\n\n🔗 Your Referral Link:\n{link}\n\n💰 Earn 10% commission from every deposit made by your referrals!"
        await update.message.reply_text(invite_msg)
        return

    if text == "🤖 Agent Panel":
        invites = get_referral_count(user_id); depositors = get_depositing_referrals_count(user_id); total_deposits = get_total_referral_deposits(user_id)
        if is_user_agent(user_id):
            agent_msg = f"🤖 AGENT DASHBOARD\n\n⭐ Status: Official Agent\n\n👥 Total Invites: {invites}\n💳 Depositing Referrals: {depositors}\n💰 Total Referral Deposits: {total_deposits} ETB\n\n🎁 Commission Rate: 10% CASH (Main Wallet)"
        else:
            agent_msg = f"🤖 AGENT UPGRADE PROGRAM\n\n⭐ Status: Normal User\n\n1️⃣ 30+ Invites\nProgress: {invites}/30\n\n2️⃣ 20+ Depositing Referrals\nProgress: {depositors}/20\n\n3️⃣ 3000+ ETB Total Referral Deposits\nProgress: {total_deposits}/3000 ETB"
        await update.message.reply_text(agent_msg)
        return

    if text in ["ℹ️ Info / መረጃ", "ℹ️ Info"]:
        await info(update, context, lang=lang); return

    if text in ["💳 Deposit / ያስገቡ", "💳 Deposit"]:
        user_state[user_id] = "deposit_amount"; await update.message.reply_text(t('deposit_prompt', lang)); return

    if text in ["🐝 Withdraw / ያውጡ", "🐝 Withdraw"]:
        if get_total_deposits(user_id) < 50: await update.message.reply_text(t('withdraw_locked', lang)); return
        user_state[user_id] = "withdraw_amount"
        await update.message.reply_text(t('withdraw_prompt', lang, play_bal=get_play_balance(user_id), main_bal=get_main_balance(user_id))); return

    if user_state.get(user_id) == "deposit_amount":
        if not text.isdigit(): await update.message.reply_text("❌ Please enter a valid number"); return
        amount = int(text)
        if amount < 10: await update.message.reply_text("❌ Minimum amount is 10 Birr"); return
        user_state[user_id] = "deposit_method"; user_state[f"{user_id}_amount"] = amount
        await update.message.reply_text("💳 Select Payment Method:" if lang == 'en' else "💳 የክፍያ ዘዴ ይምረጡ:", reply_markup=ReplyKeyboardMarkup([["Telebirr"], ["🔙 Back"]], resize_keyboard=True)); return

    if user_state.get(user_id) == "withdraw_amount":
        if not text.isdigit(): await update.message.reply_text("❌ Please enter a valid number"); return
        amount = int(text); balance = get_main_balance(user_id)
        if amount > balance: await update.message.reply_text(f"❌ Insufficient balance (Main Wallet)\n💰 You have: {balance} ETB"); return
        if amount < 100: await update.message.reply_text("❌ Minimum amount is 100 Birr"); return
        user_state[user_id] = "withdraw_method"; user_state[f"{user_id}_withdraw_amount"] = amount
        await update.message.reply_text("🏦 Select Withdraw Method:" if lang == 'en' else "🏦 የመውጣት ዘዴ ይምረጡ:", reply_markup=ReplyKeyboardMarkup([["Telebirr"], ["🔙 Back"]], resize_keyboard=True)); return

    if user_state.get(user_id) == "deposit_method":
        if text == "🔙 Back":
            await update.message.reply_text("👇 Main Menu", reply_markup=get_inline_menu(lang)); await update.message.reply_text("⬇️ Menu:", reply_markup=get_main_menu(lang))
            user_state.pop(user_id, None); user_state.pop(f"{user_id}_amount", None); return
        if text != "Telebirr": await update.message.reply_text("❌ Please choose Telebirr"); return
        amount = user_state.get(f"{user_id}_amount", 0); user_state[user_id] = "deposit_confirm"; user_state[f"{user_id}_method"] = "Telebirr"
        pay_msg = f"💳 Payment Instructions\n\nSend *{amount} Birr* to:\n\n🏦 Method: Telebirr\n📱 Phone:\n`0998480054`\n\nℹ️ After sending the money, copy the entire confirmation SMS from Telebirr and paste it here 👇"
        await update.message.reply_text(pay_msg, parse_mode="Markdown"); return

    if user_state.get(user_id) == "withdraw_method":
        if text == "🔙 Back":
            await update.message.reply_text("👇 Main Menu", reply_markup=get_inline_menu(lang)); await update.message.reply_text("⬇️ Menu:", reply_markup=get_main_menu(lang))
            user_state.pop(user_id, None); user_state.pop(f"{user_id}_withdraw_amount", None); return
        if text != "Telebirr": await update.message.reply_text("❌ Please choose Telebirr"); return
        amount = user_state.get(f"{user_id}_withdraw_amount", 0); user = get_user(user_id); user_phone = user[1] if user else "N/A"
        user_state.pop(user_id, None); user_state.pop(f"{user_id}_withdraw_amount", None)
        await update.message.reply_text("⏳ Withdraw request sent to admin", reply_markup=get_main_menu(lang))
        request_counter += 1; req_num = request_counter; withdraw_requests[req_num] = {"user_id": user_id, "amount": amount, "method": "Telebirr", "phone": user_phone}
        admin_msg = f"🚨 WITHDRAW REQUEST #{req_num}\n\n👤 User ID: {user_id}\n📱 Phone: {user_phone}\n\n💰 Amount: {amount} ETB\n🏦 Method: Telebirr\n\n✅ /ap {req_num}\n❌ /re {req_num}"
        for admin_id in ADMIN_IDS:
            try: await context.bot.send_message(chat_id=admin_id, text=admin_msg)
            except: pass
        return

    if user_state.get(user_id) == "deposit_confirm":
        amount = user_state.get(f"{user_id}_amount", 0); method = user_state.get(f"{user_id}_method", "Unknown")
        if text == "🔙 Back":
            user_state[user_id] = "deposit_method"
            await update.message.reply_text("💳 Select Payment Method:" if lang == 'en' else "💳 የክፍያ ዘዴ ይምረጡ:", reply_markup=ReplyKeyboardMarkup([["Telebirr"], ["🔙 Back"]], resize_keyboard=True)); return

        result = verify_telebirr_sms(sms_text=text, expected_amount=amount)
        if not result['valid']: await update.message.reply_text(result['reason'], parse_mode="Markdown"); return

        transaction_id = result['transaction_id']; confirmed_amount = int(result['amount']); bonus = int(confirmed_amount * 0.10); total = confirmed_amount + bonus
        _mark_transaction_used(transaction_id, user_id, confirmed_amount)
        update_play_balance(user_id, total); add_transaction(user_id, "deposit", total); new_balance = get_play_balance(user_id)

        user = get_user(user_id); ref_by = user[4] if user and len(user) > 4 else None
        if ref_by:
            if is_user_agent(int(ref_by)):
                ref_bonus = int(confirmed_amount * 0.10); update_main_balance(int(ref_by), ref_bonus)
                try: await context.bot.send_message(chat_id=int(ref_by), text=f"🤝 Agent Cash Commission!\n\n👤 Your referral deposited: {confirmed_amount} ETB\n💰 You earned: {ref_bonus} ETB (10% Cash)")
                except: pass
            else:
                ref_bonus = int(confirmed_amount * 0.10); update_play_balance(int(ref_by), ref_bonus)
                try: await context.bot.send_message(chat_id=int(ref_by), text=f"🎉 Referral Deposit Bonus!\n\n👤 Your referral deposited: {confirmed_amount} ETB\n💰 You earned: {ref_bonus} ETB (10%)")
                except: pass
            if check_and_upgrade_agent(int(ref_by)):
                try: await context.bot.send_message(chat_id=int(ref_by), text="🎉 Congratulations! You are now an Official Agent! 🤝\n\n🎁 From now on you earn 10% CASH to Main Wallet!")
                except: pass

        user_state.pop(user_id, None); user_state.pop(f"{user_id}_amount", None); user_state.pop(f"{user_id}_method", None)
        for admin_id in ADMIN_IDS:
            try: await context.bot.send_message(chat_id=admin_id, text=f"✅ DEPOSIT VERIFIED\n\n👤 User ID: {user_id}\n💰 Amount: {confirmed_amount} ETB\n🎁 Bonus: {bonus} ETB\n📈 Total: {total} ETB\n🔖 TXN: {transaction_id}")
            except: pass
        await update.message.reply_text(t('deposit_success', lang, method=method, amount=confirmed_amount, bonus=bonus, total=total, new_balance=new_balance), reply_markup=get_main_menu(lang)); return

    if text in ["🔄 Transfer / ይላኩ", "🔄 Transfer"]:
        if get_total_deposits(user_id) < 50: await update.message.reply_text("❌ Transfer locked!\n\n⚠️ You must deposit at least 50 ETB in total to unlock transfers."); return
        user_state[user_id] = "transfer_select_wallet"
        await update.message.reply_text("🔄 Select the wallet you want to send from:" if lang == 'en' else "🔄 ከየትኛው ዋሌት መላክ ይፈልጋሉ?", reply_markup=ReplyKeyboardMarkup([["Main Wallet", "Play Wallet"], ["🔙 Back"]], resize_keyboard=True)); return

    if user_state.get(user_id) == "transfer_select_wallet":
        if text == "🔙 Back": await update.message.reply_text("⬇️ Menu:", reply_markup=get_main_menu(lang)); user_state.pop(user_id, None); return
        if text not in ["Main Wallet", "Play Wallet"]: await update.message.reply_text("❌ Please choose Main Wallet or Play Wallet"); return
        user_state[f"{user_id}_transfer_wallet"] = text; user_state[user_id] = "transfer_phone"
        await update.message.reply_text("📱 Enter the registered phone number of the person you want to send to:\n\n(Example: 0912345678)", reply_markup=ReplyKeyboardMarkup([["🔙 Back"]], resize_keyboard=True)); return

    if user_state.get(user_id) == "transfer_phone":
        if text == "🔙 Back":
            user_state[user_id] = "transfer_select_wallet"
            await update.message.reply_text("🔄 Select the wallet you want to send from:" if lang == 'en' else "🔄 ከየትኛው ዋሌት መላክ ይፈልጋሉ?", reply_markup=ReplyKeyboardMarkup([["Main Wallet", "Play Wallet"], ["🔙 Back"]], resize_keyboard=True)); return
        clean_phone = normalize_phone(text); receiver_user = get_user_by_phone(clean_phone)
        if not receiver_user: await update.message.reply_text("❌ This phone number is not registered in our bot."); return
        if receiver_user[0] == user_id: await update.message.reply_text("❌ You cannot transfer money to yourself!"); return
        user_state[f"{user_id}_transfer_target"] = receiver_user[0]; user_state[user_id] = "transfer_amount"
        await update.message.reply_text("💰 Enter the amount you want to transfer (ETB):\n\nMin: 10 ETB", reply_markup=ReplyKeyboardMarkup([["🔙 Back"]], resize_keyboard=True)); return

    if user_state.get(user_id) == "transfer_amount":
        if text == "🔙 Back":
            user_state[user_id] = "transfer_phone"
            await update.message.reply_text("📱 Enter the registered phone number of the person you want to send to:\n\n(Example: 0912345678)", reply_markup=ReplyKeyboardMarkup([["🔙 Back"]], resize_keyboard=True)); return
        if not text.isdigit(): await update.message.reply_text("❌ Please enter a valid number"); return
        amount = int(text); wallet_type = user_state.get(f"{user_id}_transfer_wallet"); target_id = user_state.get(f"{user_id}_transfer_target")
        if amount < 10: await update.message.reply_text("❌ Minimum amount is 10 ETB"); return
        balance = get_main_balance(user_id) if wallet_type == "Main Wallet" else get_play_balance(user_id)
        if amount > balance: await update.message.reply_text(f"❌ Insufficient balance ({wallet_type})\n💰 Balance: {balance} ETB"); return
        if wallet_type == "Main Wallet": update_main_balance(user_id, -amount); update_main_balance(target_id, amount)
        else: update_play_balance(user_id, -amount); update_play_balance(target_id, amount)
        add_transaction(user_id, "transfer_out", amount); sender_name = update.effective_user.first_name
        try: receiver_name = (await context.bot.get_chat(target_id)).first_name
        except: receiver_name = "User"
        user_state.pop(user_id, None); user_state.pop(f"{user_id}_transfer_wallet", None); user_state.pop(f"{user_id}_transfer_target", None)
        await update.message.reply_text(f"✅ Transfer Successful!\n\n💸 Sent: {amount} ETB\n👤 To: {receiver_name}\n🏦 Wallet: {wallet_type}", reply_markup=get_main_menu(lang))
        try: await context.bot.send_message(chat_id=target_id, text=f"💰 Money Received!\n\n💸 Amount: {amount} ETB\n👤 From: {sender_name}\n🏦 Wallet: {wallet_type}")
        except: pass
        return

    await update.message.reply_text("👇 Please use the menu buttons" if lang == 'en' else "👇 የሜኑ ቁልፎችን ይጠቀሙ")

# --------------------------
# WEB APP DATA / CALLBACK / INFO / APPROVE / REJECT
# --------------------------
async def handle_web_app_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id; user_name = update.effective_user.first_name; data = update.message.web_app_data.data
    await update.message.reply_text(f"🎉 Congratulations! Your bingo result has been recorded!\n\nData: {data}")
    for admin_id in ADMIN_IDS:
        try: await context.bot.send_message(chat_id=admin_id, text=f"🎮 User {user_name} just won a Bingo game!\nData: {data}")
        except: pass

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    user_id = query.from_user.id; data = query.data; first_name = query.from_user.first_name or ''
    if first_name and user_exists(user_id): update_user_name(user_id, first_name)

    if data in ["lang_am", "lang_en"]:
        lang = 'am' if data == "lang_am" else 'en'; context.user_data["lang"] = lang
        if has_valid_phone(user_id):
            set_user_language(user_id, lang)
            try: await query.message.edit_text("✅ " + t('lang_changed', lang))
            except: pass
            await context.bot.send_message(chat_id=user_id, text=t('welcome_back', lang), reply_markup=get_main_menu(lang))
        else:
            if user_exists(user_id): set_user_language(user_id, lang)
            try: await query.message.edit_text("✅ " + ("ቋንቋ ተመርጧል!" if lang == 'am' else "Language selected!"))
            except: pass
            await context.bot.send_message(chat_id=user_id, text=t('welcome_new', lang), reply_markup=get_register_keyboard(lang))
        return

    if not has_valid_phone(user_id):
        lang = context.user_data.get("lang", 'am')
        try: await query.message.reply_text("⚠️ Please register first! Share your phone number below.", reply_markup=get_register_keyboard(lang))
        except: pass
        return

    lang = get_user_language(user_id)
    if data.startswith("menu_"):
        for k in [user_id, f"{user_id}_amount", f"{user_id}_withdraw_amount", f"{user_id}_method", f"{user_id}_transfer_wallet", f"{user_id}_transfer_target"]: user_state.pop(k, None)

    if data == "menu_open_game":
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🎲 Play Bingo Now", web_app=WebAppInfo(url=MINI_APP_URL))]])
        await query.message.reply_text("🎮 Tap the button below to open the Bingo Game:" if lang == 'en' else "🎮 የቢንጎ ጨዋታውን ለመክፈት ከታች ያለውን ቁልፍ ይጫኑ:", reply_markup=keyboard)
    elif data == "menu_balance": await query.message.reply_text(t('balance_msg', lang, main=get_main_balance(user_id), play=get_play_balance(user_id)))
    elif data == "menu_deposit": user_state[user_id] = "deposit_amount"; await query.message.reply_text(t('deposit_prompt', lang))
    elif data == "menu_withdraw":
        if get_total_deposits(user_id) < 50: await query.message.reply_text(t('withdraw_locked', lang))
        else: user_state[user_id] = "withdraw_amount"; await query.message.reply_text(t('withdraw_prompt', lang, play_bal=get_play_balance(user_id), main_bal=get_main_balance(user_id)))
    elif data == "menu_history":
        history = get_last_5_transactions(user_id)
        if not history: await query.message.reply_text("📜 No transactions yet.")
        else:
            msg = "📜 LAST 5 TRANSACTIONS\n\n"
            for tx in history: msg += f"{'🟢 Deposit' if tx[0] == 'deposit' else '🔴 Withdraw'}\n💰 Amount: {tx[1]} ETB\n⏰ Date: {tx[2].split('.')[0]}\n\n"
            await query.message.reply_text(msg)
    elif data == "menu_profile":
        user = get_user(user_id)
        if user:
            link = f"https://t.me/{BOT_USERNAME}?start={user_id}"; ref_count = get_referral_count(user_id)
            await query.message.reply_text(f"👤 PROFILE\n\n🆔 ID: {user[0]}\n📱 Phone: {user[1]}\n\n💰 Main Wallet: {user[2]} ETB\n🎮 Play Wallet: {user[3]} ETB\n\n🎯 Games Played: {get_games_played_count(user_id)}\n🏆 Games Won: {get_games_won_count(user_id)}\n💵 Total Won: {get_total_won(user_id)} ETB\n\n👥 Referrals: {ref_count}\n\n🎁 Invite Link:\n{link}")
    elif data == "menu_support": await query.message.reply_text("☎️ Support\n\nFor any comments or questions, contact support:\n@thelastking12312345678\n@Silencedoeir\n@one_day_82")
    elif data == "menu_invite":
        link = f"https://t.me/{BOT_USERNAME}?start={user_id}"; ref_count = get_referral_count(user_id)
        await query.message.reply_text(f"🎁 Invite Friends System\n\n👥 Your Invites: {ref_count}\n\n🔗 Your Referral Link:\n{link}\n\n💰 Earn 10% commission from every deposit made by your referrals!")
    elif data == "menu_agent":
        invites = get_referral_count(user_id); depositors = get_depositing_referrals_count(user_id); total_deposits = get_total_referral_deposits(user_id)
        if is_user_agent(user_id): await query.message.reply_text(f"🤖 AGENT DASHBOARD\n\n⭐ Status: Official Agent\n\n👥 Total Invites: {invites}\n💳 Depositing Referrals: {depositors}\n💰 Total Referral Deposits: {total_deposits} ETB\n\n🎁 Commission Rate: 10% CASH (Main Wallet)")
        else: await query.message.reply_text(f"🤖 AGENT UPGRADE PROGRAM\n\n⭐ Status: Normal User\n\n1️⃣ 30+ Invites\nProgress: {invites}/30\n\n2️⃣ 20+ Depositing Referrals\nProgress: {depositors}/20\n\n3️⃣ 3000+ ETB Total Referral Deposits\nProgress: {total_deposits}/3000 ETB")
    elif data == "menu_transfer":
        if get_total_deposits(user_id) < 50: await query.message.reply_text("❌ Transfer locked!\n\n⚠️ You must deposit at least 50 ETB in total to unlock transfers.")
        else: user_state[user_id] = "transfer_select_wallet"; await query.message.reply_text("🔄 Select the wallet you want to send from:" if lang == 'en' else "🔄 ከየትኛው ዋሌት መላክ ይፈልጋሉ?", reply_markup=ReplyKeyboardMarkup([["Main Wallet", "Play Wallet"], ["🔙 Back"]], resize_keyboard=True))
    elif data == "menu_info": await info(update, context, lang=lang)

async def info(update: Update, context: ContextTypes.DEFAULT_TYPE, lang=None):
    if lang is None:
        user_id = update.effective_user.id; lang = get_user_language(user_id) if user_exists(user_id) else context.user_data.get("lang", 'am')
    if lang == 'en': await update.effective_message.reply_text("☎️ Support\n\nIf you have any problems, contact @one_day_82\n\nℹ️ Information\n\n🎮 How to play\n1. Click \"Open Game\"\n2. Select your Bingo cards\n3. Follow along as numbers are called\n4. Complete a winning pattern to win!\n\nGood luck! 🍀")
    else: await update.effective_message.reply_text("☎️ Support(ድጋፍ)\n\nችግር ካጋጠመዎት @one_day_82 ን ያግኙ\n\nℹ️ Information(መረጃ)\n\n🎮 እንዴት እንደሚጫወቱ\n1. \"Play Now/ይጫወቱ\" የሚለውን ይጫኑ\n2. የቢንጎ ካርዶችዎን ይምረጡ\n3. ቁጥሮች ሲጠሩ እየተተኩ ካርዶችዎ ውስጥ ካሉ ያጥቁሩ\n4. ቢያንስ አንድ የማሸነፊያ ንድፍ ሲያጠናቅቁ \"BINGO\" ይበሉ\n\nመልካም ዕድል ይገጥምዎ! 🍀")

async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    if not context.args: await update.message.reply_text("❌ Example: /ap 1"); return
    try: req_num = int(context.args[0])
    except: await update.message.reply_text("❌ Invalid number"); return
    if req_num not in withdraw_requests: await update.message.reply_text(f"❌ Request #{req_num} not found."); return
    req_data = withdraw_requests[req_num]; user_id = req_data["user_id"]; amount = req_data["amount"]; balance = get_main_balance(user_id)
    if amount > balance: await update.message.reply_text(f"❌ Insufficient user balance. User only has {balance} ETB."); return
    update_main_balance(user_id, -amount); add_transaction(user_id, "withdraw", amount)
    await context.bot.send_message(chat_id=user_id, text=f"✅ Withdraw Approved\n💰 Amount: {amount} ETB")
    await update.message.reply_text(f"✅ Request #{req_num} Approved successfully"); del withdraw_requests[req_num]

async def reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    if not context.args: await update.message.reply_text("❌ Example: /re 1"); return
    try: req_num = int(context.args[0])
    except: await update.message.reply_text("❌ Invalid number"); return
    if req_num not in withdraw_requests: await update.message.reply_text(f"❌ Request #{req_num} not found."); return
    user_id = withdraw_requests[req_num]["user_id"]
    await context.bot.send_message(chat_id=user_id, text="❌ Withdraw Request Rejected")
    await update.message.reply_text(f"❌ Request #{req_num} Rejected successfully"); del withdraw_requests[req_num]

async def cmd_play(update, context): await handle_text(update, context, custom_text="🎮 Open Game")
async def cmd_deposit(update, context): await handle_text(update, context, custom_text="💳 Deposit")
async def cmd_balance(update, context): await handle_text(update, context, custom_text="💰 Balance")
async def cmd_withdraw(update, context): await handle_text(update, context, custom_text="🐝 Withdraw")
async def cmd_profile(update, context): await handle_text(update, context, custom_text="👤 Profile")
async def cmd_support(update, context): await handle_text(update, context, custom_text="🏢 Support")
async def cmd_invite(update, context): await handle_text(update, context, custom_text="🎁 Invite Friends")
async def cmd_transfer(update, context): await handle_text(update, context, custom_text="🔄 Transfer")
async def cmd_history(update, context): await handle_text(update, context, custom_text="📜 History")
async def cmd_agent(update, context): await handle_text(update, context, custom_text="🤖 Agent Panel")

# ==========================
# FLASK API SERVER + SOCKETIO
# ==========================
flask_app = Flask(__name__)
CORS(flask_app, resources={r"/api/*": {"origins": "*", "methods": ["GET", "POST", "OPTIONS"], "allow_headers": ["Content-Type", "ngrok-skip-browser-warning"]}})
socketio = SocketIO(flask_app, cors_allowed_origins="*", async_mode='threading')

# Wire the multiplayer engine to Socket.IO (Step 4)
socket_manager = SocketManager(socketio, room_manager, game_engine)
game_engine.start()   # background sweeper for room cleanup

@flask_app.after_request
def add_headers(response):
    response.headers['ngrok-skip-browser-warning'] = 'true'; return response

# ==========================================
# FLASK API ROUTES
# ==========================================
@flask_app.route('/')
def serve_index():
    import os as _os
    path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'index.html')
    with open(path, 'r', encoding='utf-8') as f:
        return f.read(), 200, {'Cache-Control': 'no-cache', 'Content-Type': 'text/html; charset=utf-8'}
@flask_app.route('/api/ping', methods=['GET', 'OPTIONS'])
def api_ping():
    return jsonify({'success': True, 'message': 'API is running', 'time': time_module.time()})

@flask_app.route('/download/adwa_bingo_bot.zip', methods=['GET'])
def api_download_bot():
    path = '/root/jarvis/adwa_bingo_bot.zip'
    if not os.path.exists(path):
        return jsonify({'success': False, 'error': 'zip not ready yet'}), 404
    return send_file(path, as_attachment=True, download_name='adwa_bingo_bot.zip', mimetype='application/zip')

@flask_app.route('/api/update_name', methods=['POST', 'OPTIONS'])
def api_update_name():
    if request.method == 'OPTIONS': return jsonify({'success': True}), 200
    data = request.json or {}; user_id = data.get('user_id'); first_name = data.get('first_name', '')
    if not user_id or not first_name: return jsonify({'success': False, 'error': 'user_id and first_name required'}), 400
    try: user_id = int(user_id)
    except: return jsonify({'success': False, 'error': 'invalid user_id'}), 400
    if user_exists(user_id): update_user_name(user_id, first_name)
    return jsonify({'success': True})

@flask_app.route('/api/balance', methods=['GET', 'OPTIONS'])
def api_balance():
    user_id = request.args.get('user_id', type=int)
    if not user_id: return jsonify({'success': False, 'error': 'user_id required'}), 400
    if not user_exists(user_id):
        add_user(user_id, '', '')
    user_data = db.get_user_full(user_id); status = user_data.get('status', 'active') if user_data else 'active'; is_vip = user_data.get('is_vip', 0) if user_data else 0
    return jsonify({'success': True, 'main_balance': get_main_balance(user_id), 'play_balance': get_play_balance(user_id), 'is_banned': status == 'banned', 'is_frozen': status == 'frozen', 'is_vip': is_vip == 1})

@flask_app.route('/api/bet', methods=['POST', 'OPTIONS'])
def api_bet():
    if request.method == 'OPTIONS': return jsonify({'success': True}), 200
    # Disabled: pre-dates the multiplayer engine, which deducts stakes itself
    # via deduct_bet_smart() at card-select time. Not called by index.html
    # anymore. Left registered (rather than removed) so it fails loudly
    # instead of 404ing mysteriously if something old still points at it.
    return jsonify({'success': False, 'error': 'This endpoint is disabled. Bets are placed automatically when you select a card.'}), 410

@flask_app.route('/api/win', methods=['POST', 'OPTIONS'])
def api_win():
    if request.method == 'OPTIONS': return jsonify({'success': True}), 200
    # Disabled: this took a user_id + arbitrary amount from the request body
    # and credited it directly to main_balance with NO verification a game
    # was won - anyone who found the URL could mint themselves real money.
    # The multiplayer engine now pays winners itself in game_engine.py,
    # server-side, based on the actual validated BINGO claim.
    return jsonify({'success': False, 'error': 'This endpoint is disabled. Winnings are paid automatically by the game engine.'}), 410

@flask_app.route('/api/game_played', methods=['POST', 'OPTIONS'])
def api_game_played():
    if request.method == 'OPTIONS': return jsonify({'success': True}), 200
    data = request.json or {}; user_id = data.get('user_id'); game_id = data.get('game_id', ''); cards = data.get('cards', []); entry = data.get('stake', 10)
    if not user_id: return jsonify({'success': False, 'error': 'user_id required'}), 400
    try: user_id = int(user_id)
    except: return jsonify({'success': False, 'error': 'invalid user_id'}), 400
    if not user_exists(user_id): return jsonify({'success': False, 'error': 'User not found'}), 404
    add_game_session(user_id, game_id, cards, entry); return jsonify({'success': True})

@flask_app.route('/api/game_state', methods=['GET', 'OPTIONS'])
def api_game_state():
    room = request.args.get('room', '10')
    try: stake = int(room)
    except: stake = 10
    return jsonify(legacy_state_for_stake(stake))

@flask_app.route('/api/start_game', methods=['POST', 'OPTIONS'])
def api_start_game():
    if request.method == 'OPTIONS': return jsonify({'success': True}), 200
    data = request.json or {}; room = data.get('room', '10')
    try: stake = int(room)
    except: stake = 10
    room_obj = room_manager.get_open_room(stake) or room_manager.create_room(stake)
    game_engine.start_now(room_obj.game_id)
    return jsonify({'success': True, 'game_id': room_obj.game_id})

@flask_app.route('/api/end_game', methods=['POST', 'OPTIONS'])
def api_end_game():
    if request.method == 'OPTIONS': return jsonify({'success': True}), 200
    data = request.json or {}; room = data.get('room', '10')
    try: stake = int(room)
    except: stake = 10
    room_obj = room_manager.get_open_room(stake)
    if room_obj:
        game_engine.cancel_game(room_obj.game_id, reason='game_ended')
    return jsonify({'success': True})

@flask_app.route('/api/profile_stats', methods=['GET', 'OPTIONS'])
def api_profile_stats():
    user_id = request.args.get('user_id', type=int)
    if not user_id or not user_exists(user_id): return jsonify({'success': False, 'error': 'User not found'}), 404
    user_data = db.get_user_full(user_id); is_vip = user_data.get('is_vip', 0) if user_data else 0
    return jsonify({'success': True, 'games_played': get_games_played_count(user_id), 'games_won': get_games_won_count(user_id), 'total_won': get_total_won(user_id), 'invited': get_referral_count(user_id), 'is_vip': is_vip == 1, 'main_balance': get_main_balance(user_id), 'play_balance': get_play_balance(user_id), 'total_bet': get_total_bet(user_id), 'last_played': get_last_played(user_id)})

@flask_app.route('/api/game_history', methods=['GET', 'OPTIONS'])
def api_game_history():
    user_id = request.args.get('user_id', type=int)
    if not user_id or not user_exists(user_id): return jsonify({'success': False, 'error': 'User not found'}), 404
    return jsonify({'success': True, 'history': [{'game_id': r[0], 'entry': r[1], 'status': r[2], 'result': r[3], 'time': r[4]} for r in get_game_history(user_id, limit=20)]})

@flask_app.route('/api/transactions', methods=['GET', 'OPTIONS'])
def api_transactions():
    user_id = request.args.get('user_id', type=int)
    if not user_id or not user_exists(user_id): return jsonify({'success': False, 'error': 'User not found'}), 404
    return jsonify({'success': True, 'transactions': [{'type': r[0], 'amount': r[1], 'status': r[2], 'time': r[3]} for r in get_all_transactions(user_id, limit=20)]})

@flask_app.route('/api/top_winners', methods=['GET', 'OPTIONS'])
def api_top_winners():
    period = request.args.get('period', 'week'); category = request.args.get('category', 'deposit')
    if category == 'deposit': rows = get_top_by_deposit(period, 30)
    elif category == 'invite': rows = get_top_by_invitations(period, 30)
    elif category == 'wins': rows = get_top_by_wins(period, 30)
    else: rows = get_top_by_games(period, 30)
    return jsonify({'success': True, 'winners': [{'name': r[1] if r[1] and r[1].strip() else 'User', 'value': r[2]} for r in rows]})

@flask_app.route('/api/my_rank', methods=['GET', 'OPTIONS'])
def api_my_rank():
    user_id = request.args.get('user_id', type=int); period = request.args.get('period', 'week'); category = request.args.get('category', 'deposit')
    if not user_id or not user_exists(user_id): return jsonify({'success': False, 'error': 'User not found'}), 404
    rank, value = get_user_rank(user_id, period, category); return jsonify({'success': True, 'rank': rank, 'value': value})

# ══════════════════════════════════════════════════════════
# ADMIN FLASK ROUTES
# ══════════════════════════════════════════════════════════
@flask_app.route('/api/admin/dashboard', methods=['GET', 'OPTIONS'])
def api_admin_dashboard():
    if request.method == 'OPTIONS': return jsonify({'success': True}), 200
    try:
        stats = db.get_dashboard_stats()
        stats['active_online'] = room_manager.active_cards_total()
        stats['running_games'] = room_manager.running_games_count()
        stats['success'] = True
        return jsonify(stats)
    except Exception as e: return jsonify({'success': False, 'error': str(e)}), 500

@flask_app.route('/api/admin/deposits', methods=['GET', 'OPTIONS'])
def api_admin_deposits():
    if request.method == 'OPTIONS': return jsonify({'success': True}), 200
    try: return jsonify({'success': True, 'deposits': db.get_all_deposits()})
    except Exception as e: return jsonify({'success': False, 'error': str(e)}), 500

@flask_app.route('/api/admin/approve_deposit', methods=['POST', 'OPTIONS'])
def api_approve_deposit():
    if request.method == 'OPTIONS': return jsonify({'success': True}), 200
    data = request.json or {}; success, user_id, amount = db.approve_deposit(data.get('deposit_id')); return jsonify({'success': success})

@flask_app.route('/api/admin/reject_deposit', methods=['POST', 'OPTIONS'])
def api_reject_deposit():
    if request.method == 'OPTIONS': return jsonify({'success': True}), 200
    data = request.json or {}; success, user_id = db.reject_deposit(data.get('deposit_id')); return jsonify({'success': success})

@flask_app.route('/api/admin/withdrawals', methods=['GET', 'OPTIONS'])
def api_admin_withdrawals():
    if request.method == 'OPTIONS': return jsonify({'success': True}), 200
    try:
        withdrawals = db.get_all_withdrawals()
        for req_num, req in withdraw_requests.items(): withdrawals.append({'id': req_num, 'user_id': req['user_id'], 'username': '—', 'phone': req.get('phone', '—'), 'amount': req['amount'], 'method': req.get('method', 'Telebirr'), 'status': 'pending', 'time': time_module.strftime('%Y-%m-%d %H:%M:%S')})
        return jsonify({'success': True, 'withdrawals': withdrawals})
    except Exception as e: return jsonify({'success': False, 'error': str(e)}), 500

@flask_app.route('/api/admin/approve_withdrawal', methods=['POST', 'OPTIONS'])
def api_approve_withdrawal():
    if request.method == 'OPTIONS': return jsonify({'success': True}), 200
    data = request.json or {}; withdrawal_id = data.get('withdrawal_id'); user_id = data.get('user_id'); amount = data.get('amount', 0)
    db.update_main_balance(user_id, -amount); db.add_transaction(user_id, 'withdraw', amount)
    try:
        import asyncio
        asyncio.run(app.bot.send_message(chat_id=user_id, text=f"✅ Withdrawal Approved!\n\n💰 Amount: {amount} ETB\n🏦 The money has been sent to your account."))
    except: pass
    if withdrawal_id in withdraw_requests: del withdraw_requests[withdrawal_id]
    else: db.approve_withdrawal(withdrawal_id)
    return jsonify({'success': True})

@flask_app.route('/api/admin/reject_withdrawal', methods=['POST', 'OPTIONS'])
def api_reject_withdrawal():
    if request.method == 'OPTIONS': return jsonify({'success': True}), 200
    data = request.json or {}; withdrawal_id = data.get('withdrawal_id'); user_id = data.get('user_id'); amount = data.get('amount', 0)
    try:
        import asyncio
        asyncio.run(app.bot.send_message(chat_id=user_id, text=f"❌ Withdrawal Rejected\n\n💰 Amount: {amount} ETB\n⚠️ Your request was rejected by admin. The money remains in your Main Wallet."))
    except: pass
    if withdrawal_id in withdraw_requests: del withdraw_requests[withdrawal_id]
    else: db.reject_withdrawal(withdrawal_id)
    return jsonify({'success': True})

@flask_app.route('/api/admin/users', methods=['GET', 'OPTIONS'])
def api_admin_users():
    if request.method == 'OPTIONS': return jsonify({'success': True}), 200
    try: return jsonify({'success': True, 'users': db.get_all_users_with_stats()})
    except Exception as e: return jsonify({'success': False, 'error': str(e)}), 500

@flask_app.route('/api/admin/add_balance', methods=['POST', 'OPTIONS'])
def api_add_balance():
    if request.method == 'OPTIONS': return jsonify({'success': True}), 200
    data = request.json or {}; user_id = data.get('user_id'); amount = data.get('amount', 0); new_bal = db.update_main_balance(user_id, amount); db.add_transaction(user_id, 'admin_add', amount); return jsonify({'success': True, 'main_balance': new_bal, 'play_balance': db.get_play_balance(user_id)})

@flask_app.route('/api/admin/remove_balance', methods=['POST', 'OPTIONS'])
def api_remove_balance():
    if request.method == 'OPTIONS': return jsonify({'success': True}), 200
    data = request.json or {}; user_id = data.get('user_id'); amount = data.get('amount', 0); new_bal = db.update_main_balance(user_id, -amount); db.add_transaction(user_id, 'admin_remove', amount); return jsonify({'success': True, 'main_balance': new_bal, 'play_balance': db.get_play_balance(user_id)})

@flask_app.route('/api/admin/add_main_balance', methods=['POST', 'OPTIONS'])
def api_add_main_balance():
    if request.method == 'OPTIONS': return jsonify({'success': True}), 200
    data = request.json or {}; user_id = data.get('user_id'); amount = data.get('amount', 0); new_bal = db.update_main_balance(user_id, amount); db.add_transaction(user_id, 'admin_add_main', amount); return jsonify({'success': True, 'main_balance': new_bal, 'play_balance': db.get_play_balance(user_id)})

@flask_app.route('/api/admin/add_play_balance', methods=['POST', 'OPTIONS'])
def api_add_play_balance():
    if request.method == 'OPTIONS': return jsonify({'success': True}), 200
    data = request.json or {}; user_id = data.get('user_id'); amount = data.get('amount', 0); new_bal = db.update_play_balance(user_id, amount); db.add_transaction(user_id, 'admin_add_play', amount); return jsonify({'success': True, 'main_balance': db.get_main_balance(user_id), 'play_balance': new_bal})

@flask_app.route('/api/admin/remove_main_balance', methods=['POST', 'OPTIONS'])
def api_remove_main_balance():
    if request.method == 'OPTIONS': return jsonify({'success': True}), 200
    data = request.json or {}; user_id = data.get('user_id'); amount = data.get('amount', 0); new_bal = db.update_main_balance(user_id, -amount); db.add_transaction(user_id, 'admin_remove_main', amount); return jsonify({'success': True, 'main_balance': new_bal, 'play_balance': db.get_play_balance(user_id)})

@flask_app.route('/api/admin/remove_play_balance', methods=['POST', 'OPTIONS'])
def api_remove_play_balance():
    if request.method == 'OPTIONS': return jsonify({'success': True}), 200
    data = request.json or {}; user_id = data.get('user_id'); amount = data.get('amount', 0); new_bal = db.update_play_balance(user_id, -amount); db.add_transaction(user_id, 'admin_remove_play', amount); return jsonify({'success': True, 'main_balance': db.get_main_balance(user_id), 'play_balance': new_bal})

@flask_app.route('/api/admin/ban_user', methods=['POST', 'OPTIONS'])
def api_ban_user():
    if request.method == 'OPTIONS': return jsonify({'success': True}), 200
    db.ban_user(request.json.get('user_id') if request.json else None); return jsonify({'success': True})

@flask_app.route('/api/admin/unban_user', methods=['POST', 'OPTIONS'])
def api_unban_user():
    if request.method == 'OPTIONS': return jsonify({'success': True}), 200
    db.unban_user(request.json.get('user_id') if request.json else None); return jsonify({'success': True})

@flask_app.route('/api/admin/freeze_user', methods=['POST', 'OPTIONS'])
def api_freeze_user():
    if request.method == 'OPTIONS': return jsonify({'success': True}), 200
    db.freeze_user(request.json.get('user_id') if request.json else None); return jsonify({'success': True})

@flask_app.route('/api/admin/unfreeze_user', methods=['POST', 'OPTIONS'])
def api_unfreeze_user():
    if request.method == 'OPTIONS': return jsonify({'success': True}), 200
    db.unfreeze_user(request.json.get('user_id') if request.json else None); return jsonify({'success': True})

@flask_app.route('/api/admin/mark_vip', methods=['POST', 'OPTIONS'])
def api_mark_vip():
    if request.method == 'OPTIONS': return jsonify({'success': True}), 200
    data = request.json or {}; db.mark_vip(data.get('user_id'), data.get('vip', True)); return jsonify({'success': True})

@flask_app.route('/api/admin/manual_call', methods=['POST', 'OPTIONS'])
def api_manual_call():
    if request.method == 'OPTIONS': return jsonify({'success': True}), 200
    data = request.json or {}; room = data.get('room', '10')
    try: stake = int(room)
    except: stake = 10
    room_obj = room_manager.get_open_room(stake) or room_manager.create_room(stake)
    result = game_engine.call_specific(room_obj.game_id, data.get('number'), manual=True)
    if not result['ok']: return jsonify({'success': False, 'error': result['error']}), 400
    return jsonify({'success': True, 'number': result['number'], 'room': room})

@flask_app.route('/api/admin/set_max_winners', methods=['POST', 'OPTIONS'])
def api_set_max_winners():
    if request.method == 'OPTIONS': return jsonify({'success': True}), 200
    data = request.json or {}; room = data.get('room', '10')
    try: stake = int(room)
    except: stake = 10
    room_obj = room_manager.get_open_room(stake) or room_manager.create_room(stake)
    result = game_engine.set_max_winners(room_obj.game_id, data.get('max_winners', 1))
    return jsonify({'success': result['ok'], 'max_winners': result.get('max_winners'), 'room': room})

@flask_app.route('/api/admin/pause_game', methods=['POST', 'OPTIONS'])
def api_pause_game():
    if request.method == 'OPTIONS': return jsonify({'success': True}), 200
    data = request.json or {}; room = data.get('room', '10')
    try: stake = int(room)
    except: stake = 10
    room_obj = room_manager.get_open_room(stake)
    if room_obj:
        result = game_engine.pause_resume(room_obj.game_id)
        return jsonify({'success': result['ok'], 'paused': result.get('paused'), 'room': room})
    return jsonify({'success': False, 'error': 'No active game'}), 400

@flask_app.route('/api/admin/cancel_game', methods=['POST', 'OPTIONS'])
def api_cancel_game():
    if request.method == 'OPTIONS': return jsonify({'success': True}), 200
    data = request.json or {}; room = data.get('room', '10')
    try: stake = int(room)
    except: stake = 10
    room_obj = room_manager.get_open_room(stake)
    if room_obj:
        game_engine.cancel_game(room_obj.game_id, reason='admin_cancelled')
    return jsonify({'success': True, 'room': room})

@flask_app.route('/api/admin/rankings', methods=['GET', 'OPTIONS'])
def api_admin_rankings():
    if request.method == 'OPTIONS': return jsonify({'success': True}), 200
    category = request.args.get('category', 'deposit'); period = request.args.get('period', 'week'); limit = int(request.args.get('limit', 30))
    if category == 'deposit': rows = get_top_by_deposit(period, limit)
    elif category == 'invite': rows = get_top_by_invitations(period, limit)
    elif category == 'wins': rows = get_top_by_wins(period, limit)
    else: rows = get_top_by_games(period, limit)
    return jsonify({'success': True, 'rankings': [{'user_id': r[0], 'name': r[1] or 'User', 'phone': get_user_phone(r[0]) or '—', 'value': r[2]} for r in rows]})

@flask_app.route('/api/admin/game_history', methods=['GET', 'OPTIONS'])
def api_admin_game_history():
    if request.method == 'OPTIONS': return jsonify({'success': True}), 200
    try: return jsonify({'success': True, 'games': db.get_admin_game_history()})
    except Exception as e: return jsonify({'success': False, 'error': str(e)}), 500

@flask_app.route('/api/admin/reports', methods=['GET', 'OPTIONS'])
def api_admin_reports():
    if request.method == 'OPTIONS': return jsonify({'success': True}), 200
    try: return jsonify({'success': True, 'rows': db.get_admin_reports(request.args.get('period', 'daily'))})
    except Exception as e: return jsonify({'success': False, 'error': str(e)}), 500

@flask_app.route('/api/admin/settings', methods=['POST', 'OPTIONS'])
def api_admin_settings():
    if request.method == 'OPTIONS': return jsonify({'success': True}), 200
    data = request.json or {}; print(f"⚙️ Settings updated by {data.get('admin','admin')}: {data}"); return jsonify({'success': True})

# ══════════════════════════════════════════════════════════
# FLASK SERVER RUNNER
# ══════════════════════════════════════════════════════════
def run_flask():
    # Render assigns a random $PORT; fall back to 5000 locally
    port = int(os.environ.get("PORT", "5000"))
    socketio.run(flask_app, host='0.0.0.0', port=port, debug=False, use_reloader=False, allow_unsafe_werkzeug=True)

_services_started = False

# ==========================
# APP SETUP
# ==========================
def build_app():
    """Build the Telegram Application (fresh copy per call, used by run_local retries)."""
    PROXY_URL = None
    builder = ApplicationBuilder().token(TOKEN)
    if PROXY_URL: builder = builder.proxy(PROXY_URL).get_updates_proxy(PROXY_URL)
    builder = builder.connect_timeout(60.0).read_timeout(60.0).write_timeout(60.0).pool_timeout(60.0)
    app = builder.build()

    async def change_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🇪🇹 አማርኛ", callback_data="lang_am"), InlineKeyboardButton("🇸🇸 English", callback_data="lang_en")]])
        await update.message.reply_text(t('select_language', 'en'), reply_markup=keyboard)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("lang", change_lang))
    app.add_handler(CommandHandler("info", info))
    app.add_handler(CommandHandler("ap", approve))
    app.add_handler(CommandHandler("re", reject))
    app.add_handler(CommandHandler("play", cmd_play))
    app.add_handler(CommandHandler("deposit", cmd_deposit))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("withdraw", cmd_withdraw))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("support", cmd_support))
    app.add_handler(CommandHandler("invite", cmd_invite))
    app.add_handler(CommandHandler("transfer", cmd_transfer))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("agent", cmd_agent))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_web_app_data))
    app.add_handler(MessageHandler(filters.CONTACT, get_contact))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return app


def run_bot():
    """Telegram bot poller, run in a background thread so it works no matter
    how the process was started (python main.py, gunicorn, Render, etc.)."""
    import asyncio as _asyncio
    import telegram.error as _tg_err

    # Register the bot command list. Network flakiness (this box drops
    # connections often) must not take the whole bot down: retry, then give
    # up silently and keep polling.
    for _try in range(3):
        try:
            _asyncio.run(app.bot.set_my_commands([
                ("start", "Main menu / ዋና ማውጫ"),
                ("lang", "Change language / ቋንቋ ይቀይሩ"),
                ("play", "Open Game / ይጫወቱ"),
                ("deposit", "Deposit / ያስገቡ"),
                ("balance", "Balance / ሂሳብ"),
                ("withdraw", "Withdraw / ያውጡ"),
                ("history", "History / ታሪክ"),
                ("profile", "Profile / መገለጫ"),
                ("support", "Support / ድጋፍ"),
                ("invite", "Invite Friends / ጓደኛ ይጋብዙ"),
                ("agent", "Agent Panel / ወኪል"),
                ("transfer", "Transfer / ይላኩ"),
                ("info", "Info & Rules / መረጃ"),
            ]))
            print("✅ Bot commands registered")
            break
        except _tg_err.NetworkError as e:
            print(f"⚠️ Network error registering commands ({e}); retry {_try+1}/3")
            import time as _time
            _time.sleep(5)
        except Exception as e:
            print(f"⚠️ Failed to register commands: {e}")
            break

    # Poll forever. If the flaky network kills the poller, rebuild a fresh
    # app (clean event loop) and restart instead of letting the process exit.
    # stop_signals=None is REQUIRED: run_polling registers signal handlers,
    # which only works in the main thread (Render runs this in a worker
    # thread -> ValueError "signal only works in main thread" otherwise).
    import time as _time
    while True:
        try:
            _fresh = build_app()
            _fresh.run_polling(stop_signals=None)
            break
        except Exception as e:
            print(f"[main] poller died ({type(e).__name__}: {e}); restarting in 5s...")
            _time.sleep(5)


def start_services():
    """Start Flask/Socket.IO and the Telegram bot in background threads (idempotent)."""
    global _services_started
    if _services_started:
        return
    _services_started = True
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    print("✅ Bot running with MULTIPLAYER engine (dynamic rooms, server-authoritative numbers/validation)")


app = build_app()
start_services()

if __name__ == '__main__':
    # Both Flask and the Telegram poller run in background threads started
    # above (start_services), so this process just needs to stay alive.
    # Works with `python main.py`, gunicorn, Render, etc.
    import time as _time
    while True:
        _time.sleep(3600)
