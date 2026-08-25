import os
from datetime import datetime, timedelta
from pymongo import MongoClient
from pymongo import ReturnDocument

# ══ CONNECT TO REAL MONGODB (Atlas) ONLY ══
# No local file fallback: the bot always talks to the real MongoDB
# configured via MONGO_URI in .env.
MONGO_URI = os.environ.get("MONGO_URI")
if not MONGO_URI:
    raise RuntimeError("MONGO_URI is not set. Set the real MongoDB URI in .env")
client = MongoClient(MONGO_URI)
db = client["adwa_bingo"]

# Collections (Like Tables)
users_col = db["users"]
transactions_col = db["transactions"]
game_sessions_col = db["game_sessions"]
counters_col = db["counters"]

# ══ MULTIPLAYER COLLECTIONS (Step 6 upgrade) ══
games_col = db["games"]                 # one document per multiplayer game
game_players_col = db["game_players"]   # player membership per game
bingo_cards_col = db["bingo_cards"]     # server-generated cards per game
game_history_col = db["game_history"]   # finished games archive
open_rooms_col = db["open_rooms"]       # one doc per stake: {stake, game_id} —
                                         # the ONE shared pointer to "which room
                                         # is currently open", read/written by
                                         # every server process/instance

_db_source = globals().get('MONGO_URI') or 'local'
print(f"✅ Connected to MongoDB ({_db_source.split('@')[-1].split('/')[0]})")

# ══ HELPER FOR AUTO-INCREMENT IDS ══
def get_next_id(name):
    result = counters_col.find_one_and_update(
        {"_id": name},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=True
    )
    return result["seq"]


# ==========================================
# USER FUNCTIONS
# ==========================================

def add_user(user_id, phone='', first_name='', referred_by=None):
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    users_col.update_one(
        {"user_id": user_id},
        {"$setOnInsert": {"phone": phone, "first_name": first_name, "referred_by": referred_by, "status": "active", "created_at": now, "main_balance": 0, "play_balance": 0, "is_agent": 0, "is_vip": 0, "language": "am"}},
        upsert=True
    )
    if first_name:
        users_col.update_one({"user_id": user_id, "$or": [{"first_name": None}, {"first_name": ""}]}, {"$set": {"first_name": first_name}})

def update_user_name(user_id, first_name):
    users_col.update_one({"user_id": user_id}, {"$set": {"first_name": first_name}})

def grant_welcome_bonus(user_id, amount=1000):
    """Deprecated: welcome bonuses are disabled. Kept as a no-op so legacy
    callers (if any) never crash; nothing grants balance anymore."""
    return False

def user_exists(user_id):
    return users_col.find_one({"user_id": user_id}) is not None

def has_valid_phone(user_id):
    """True when the user completed phone registration (fixes bot import)."""
    u = users_col.find_one({"user_id": user_id}, {"phone": 1})
    return bool(u and u.get("phone"))

def get_user(user_id):
    u = users_col.find_one({"user_id": user_id})
    if not u: return None
    return (u.get("user_id"), u.get("phone"), u.get("main_balance", 0), u.get("play_balance", 0), u.get("referred_by"))

def get_user_full(user_id):
    u = users_col.find_one({"user_id": user_id})
    if not u: return None
    return {
        'user_id': u.get("user_id"),
        'first_name': u.get("first_name", ''),
        'phone': u.get("phone", ''),
        'main_balance': u.get("main_balance", 0),
        'play_balance': u.get("play_balance", 0),
        'referred_by': u.get("referred_by"),
        'is_agent': u.get("is_agent", 0),
        'is_vip': u.get("is_vip", 0),
        'language': u.get("language", 'am'),
        'status': u.get("status", 'active'),
        'created_at': u.get("created_at", ''),
    }

def get_user_name(user_id):
    u = users_col.find_one({"user_id": user_id})
    return u.get("first_name", 'User') if u else 'User'

def get_user_phone(user_id):
    u = users_col.find_one({"user_id": user_id})
    return u.get("phone") if u else None


# ==========================================
# BALANCE FUNCTIONS
# ==========================================

def get_main_balance(user_id):
    u = users_col.find_one({"user_id": user_id})
    return u.get("main_balance", 0) if u else 0

def update_main_balance(user_id, amount):
    users_col.update_one({"user_id": user_id}, {"$inc": {"main_balance": amount}})
    u = users_col.find_one({"user_id": user_id})
    return u.get("main_balance", 0) if u else 0

def get_play_balance(user_id):
    u = users_col.find_one({"user_id": user_id})
    return u.get("play_balance", 0) if u else 0

def update_play_balance(user_id, amount):
    users_col.update_one({"user_id": user_id}, {"$inc": {"play_balance": amount}})
    u = users_col.find_one({"user_id": user_id})
    return u.get("play_balance", 0) if u else 0

def deduct_bet_smart(user_id, amount):
    """
    Deduct `amount`, using play_balance first and spilling into
    main_balance for any remainder - same policy as before, but now done
    with conditional atomic updates instead of read-then-write. Two
    requests from the same user landing at nearly the same instant (double
    tap, two tabs, a retried request) can no longer both read "enough
    funds" and both succeed, which is how a balance could go negative
    under the old read/decide/write version.
    """
    doc = users_col.find_one_and_update(
        {"user_id": user_id, "play_balance": {"$gte": amount}},
        {"$inc": {"play_balance": -amount}},
        return_document=ReturnDocument.AFTER,
    )
    if doc:
        return {'main_balance': doc.get('main_balance', 0), 'play_balance': doc.get('play_balance', 0)}

    user = users_col.find_one({"user_id": user_id})
    if not user:
        return False
    play_bal = max(user.get('play_balance', 0), 0)
    main_bal = user.get('main_balance', 0)
    remaining = amount - play_bal
    if remaining < 0 or play_bal + main_bal < amount:
        return False
    doc = users_col.find_one_and_update(
        {"user_id": user_id, "play_balance": play_bal, "main_balance": {"$gte": remaining}},
        {"$inc": {"play_balance": -play_bal, "main_balance": -remaining}},
        return_document=ReturnDocument.AFTER,
    )
    if not doc:
        return False   # balance changed between our read and write - fail safe, caller treats as insufficient funds
    return {'main_balance': doc.get('main_balance', 0), 'play_balance': doc.get('play_balance', 0)}


# ==========================================
# REFERRAL FUNCTIONS
# ==========================================

def set_referral(user_id, referred_by):
    users_col.update_one({"user_id": user_id, "referred_by": None}, {"$set": {"referred_by": referred_by}})

def get_referral_count(user_id):
    return users_col.count_documents({"referred_by": user_id})

def get_depositing_referrals_count(user_id):
    invited_ids = [u["user_id"] for u in users_col.find({"referred_by": user_id}, {"user_id": 1})]
    return transactions_col.count_documents({"user_id": {"$in": invited_ids}, "type": "deposit", "status": "completed"})

def get_total_referral_deposits(user_id):
    invited_ids = [u["user_id"] for u in users_col.find({"referred_by": user_id}, {"user_id": 1})]
    pipeline = [
        {"$match": {"user_id": {"$in": invited_ids}, "type": "deposit", "status": "completed"}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}}
    ]
    result = list(transactions_col.aggregate(pipeline))
    return result[0]["total"] if result else 0


# ==========================================
# TRANSACTION FUNCTIONS
# ==========================================

def add_transaction(user_id, tx_type, amount, method="System", tx_id=None, status='completed'):
    if tx_id is None and tx_type in ('deposit', 'withdraw'):
        tx_id = f"{tx_type.upper()}_{user_id}_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}"
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    row_id = get_next_id("transactions")
    transactions_col.insert_one({
        "id": row_id, "user_id": user_id, "type": tx_type, "amount": amount,
        "method": method, "tx_id": tx_id, "status": status, "time": now
    })
    return row_id

def update_transaction_status(tx_id_or_id, new_status):
    if isinstance(tx_id_or_id, int):
        result = transactions_col.update_one({"id": tx_id_or_id}, {"$set": {"status": new_status}})
    else:
        result = transactions_col.update_one({"tx_id": tx_id_or_id}, {"$set": {"status": new_status}})
    return result.modified_count > 0

def get_transaction_by_id(tx_id):
    if isinstance(tx_id, int):
        t = transactions_col.find_one({"id": tx_id})
    else:
        t = transactions_col.find_one({"tx_id": tx_id})
    return t

def get_last_5_transactions(user_id):
    txs = transactions_col.find({"user_id": user_id, "status": "completed"}).sort("id", -1).limit(5)
    return [(t["type"], t["amount"], t["time"]) for t in txs]

def get_all_transactions(user_id, limit=20):
    txs = transactions_col.find({"user_id": user_id}).sort("id", -1).limit(limit)
    return [(t["type"], t["amount"], t["status"], t["time"]) for t in txs]

def get_total_deposits(user_id):
    pipeline = [
        {"$match": {"user_id": user_id, "type": "deposit", "status": "completed"}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}}
    ]
    result = list(transactions_col.aggregate(pipeline))
    return result[0]["total"] if result else 0

def transaction_exists(tx_id):
    if not tx_id: return False
    return transactions_col.find_one({"tx_id": tx_id}) is not None


# ==========================================
# DEPOSIT FUNCTIONS (Admin)
# ==========================================

def add_pending_deposit(user_id, amount, method='Telebirr', tx_id=None, phone=None):
    if phone:
        users_col.update_one({"user_id": user_id, "$or": [{"phone": None}, {"phone": ""}]}, {"$set": {"phone": phone}})
    return add_transaction(user_id, 'deposit', amount, method, tx_id, status='pending')

def approve_deposit(transaction_id):
    t = transactions_col.find_one({"id": transaction_id})
    if not t: return False, None, 0
    if t["status"] != 'pending': return False, t["user_id"], t["amount"]
    transactions_col.update_one({"id": transaction_id}, {"$set": {"status": "completed"}})
    users_col.update_one({"user_id": t["user_id"]}, {"$inc": {"play_balance": t["amount"]}})
    return True, t["user_id"], t["amount"]

def reject_deposit(transaction_id):
    t = transactions_col.find_one({"id": transaction_id})
    if not t: return False, None
    if t["status"] != 'pending': return False, t["user_id"]
    transactions_col.update_one({"id": transaction_id}, {"$set": {"status": "rejected"}})
    return True, t["user_id"]

def get_all_deposits(limit=200):
    pipeline = [
        {"$match": {"type": "deposit"}},
        {"$sort": {"id": -1}},
        {"$limit": limit},
        {"$lookup": {"from": "users", "localField": "user_id", "foreignField": "user_id", "as": "user_info"}},
        {"$unwind": {"path": "$user_info", "preserveNullAndEmptyArrays": True}}
    ]
    deposits = []
    for t in transactions_col.aggregate(pipeline):
        u = t.get("user_info") or {}
        deposits.append({
            'id': t["id"], 'user_id': t["user_id"], 'username': u.get("first_name", '—'),
            'phone': u.get("phone", '—'), 'amount': t["amount"], 'method': t.get("method", 'Telebirr'),
            'tx_id': t.get("tx_id", '—'), 'status': t.get("status", 'pending'), 'time': t.get("time", '')
        })
    return deposits

def get_pending_deposits_count():
    return transactions_col.count_documents({"type": "deposit", "status": "pending"})


# ==========================================
# WITHDRAWAL FUNCTIONS (Admin)
# ==========================================

def add_pending_withdrawal(user_id, amount, method='Telebirr', phone=None):
    main_bal = get_main_balance(user_id)
    if main_bal < amount: return None
    update_main_balance(user_id, -amount)
    return add_transaction(user_id, 'withdraw', amount, method, status='pending')

def approve_withdrawal(transaction_id):
    t = transactions_col.find_one({"id": transaction_id})
    if not t: return False, None, 0
    if t["status"] != 'pending': return False, t["user_id"], t["amount"]
    transactions_col.update_one({"id": transaction_id}, {"$set": {"status": "completed"}})
    return True, t["user_id"], t["amount"]

def reject_withdrawal(transaction_id):
    t = transactions_col.find_one({"id": transaction_id})
    if not t: return False, None, 0
    if t["status"] != 'pending': return False, t["user_id"], t["amount"]
    transactions_col.update_one({"id": transaction_id}, {"$set": {"status": "rejected"}})
    users_col.update_one({"user_id": t["user_id"]}, {"$inc": {"main_balance": t["amount"]}})
    return True, t["user_id"], t["amount"]

def get_all_withdrawals(limit=100):
    pipeline = [
        {"$match": {"type": "withdraw"}},
        {"$sort": {"id": -1}},
        {"$limit": limit},
        {"$lookup": {"from": "users", "localField": "user_id", "foreignField": "user_id", "as": "user_info"}},
        {"$unwind": {"path": "$user_info", "preserveNullAndEmptyArrays": True}}
    ]
    withdrawals = []
    for t in transactions_col.aggregate(pipeline):
        u = t.get("user_info") or {}
        withdrawals.append({
            'id': t["id"], 'user_id': t["user_id"], 'username': u.get("first_name", '—'),
            'phone': u.get("phone", '—'), 'amount': t["amount"], 'method': t.get("method", 'Telebirr'),
            'status': t.get("status", 'pending'), 'time': t.get("time", '')
        })
    return withdrawals

def get_pending_withdrawals_count():
    return transactions_col.count_documents({"type": "withdraw", "status": "pending"})


# ==========================================
# GAME SESSION FUNCTIONS
# ==========================================

def add_game_session(user_id, game_id, cards, entry_amount=10):
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    if isinstance(cards, list) and len(cards) > 0:
        for card in cards:
            game_sessions_col.insert_one({
                "user_id": user_id, "game_id": game_id, "cards": str(card),
                "entry_amount": entry_amount, "status": "playing", "result": "-",
                "prize": 0, "time": now
            })
    else:
        game_sessions_col.insert_one({
            "user_id": user_id, "game_id": game_id, "cards": str(cards),
            "entry_amount": entry_amount, "status": "playing", "result": "-",
            "prize": 0, "time": now
        })

def complete_game_session(user_id, game_id, result='-', prize=0, cards='-', entry_amount=0):
    status = 'Won' if prize > 0 else 'Completed'
    result_str = result if prize > 0 else '-'
    game_sessions_col.update_one(
        {"user_id": user_id, "game_id": game_id, "cards": str(cards)},
        {"$set": {
            "user_id": user_id, "game_id": game_id, "cards": str(cards),
            "entry_amount": entry_amount, "status": status, "result": result_str,
            "prize": prize, "time": datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        }},
        upsert=True
    )

def get_game_history(user_id, limit=20):
    sessions = game_sessions_col.find({"user_id": user_id}).sort("time", -1).limit(limit)
    return [(s["game_id"], s["entry_amount"], s["status"], s["result"], s["time"]) for s in sessions]

def get_games_played_count(user_id):
    return game_sessions_col.count_documents({"user_id": user_id})

def get_total_bet(user_id):
    return sum(t.get("amount", 0) for t in transactions_col.find({"user_id": user_id, "type": "bingo_bet"}))

def get_last_played(user_id):
    sessions = game_sessions_col.find({"user_id": user_id}).sort("time", -1).limit(1)
    for s in sessions:
        return s.get("time")
    return None

def get_games_won_count(user_id):
    return game_sessions_col.count_documents({"user_id": user_id, "status": "Won"})

def get_total_won(user_id):
    pipeline = [
        {"$match": {"user_id": user_id, "status": "Won"}},
        {"$group": {"_id": None, "total": {"$sum": "$prize"}}}
    ]
    result = list(game_sessions_col.aggregate(pipeline))
    return result[0]["total"] if result else 0


# ==========================================
# PROFILE STATS
# ==========================================

def get_profile_stats(user_id):
    return {
        'games_played': get_games_played_count(user_id),
        'games_won': get_games_won_count(user_id),
        'total_won': get_total_won(user_id),
        'invited': get_referral_count(user_id),
    }


# ==========================================
# LEADERBOARD FUNCTIONS
# ==========================================

def get_period_start(period):
    now = datetime.utcnow()
    if period == 'week':
        start = now - timedelta(days=now.weekday())
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == 'month':
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        start = now - timedelta(days=7)
    return start.strftime('%Y-%m-%d %H:%M:%S')

def get_top_by_deposit(period='week', limit=30):
    since = get_period_start(period)
    pipeline = [
        {"$match": {"type": "deposit", "status": "completed", "time": {"$gte": since}}},
        {"$group": {"_id": "$user_id", "total": {"$sum": 1}}},
        {"$sort": {"total": -1}},
        {"$limit": limit},
        {"$lookup": {"from": "users", "localField": "_id", "foreignField": "user_id", "as": "user_info"}},
        {"$unwind": "$user_info"}
    ]
    return [(r["_id"], r["user_info"].get("first_name", "User"), r["total"]) for r in transactions_col.aggregate(pipeline)]

def get_top_by_invitations(period='week', limit=30):
    since = get_period_start(period)
    pipeline = [
        {"$match": {"created_at": {"$gte": since}}},
        {"$group": {"_id": "$referred_by", "total": {"$sum": 1}}},
        {"$match": {"_id": {"$ne": None}, "total": {"$gt": 0}}},
        {"$sort": {"total": -1}},
        {"$limit": limit},
        {"$lookup": {"from": "users", "localField": "_id", "foreignField": "user_id", "as": "user_info"}},
        {"$unwind": "$user_info"}
    ]
    return [(r["_id"], r["user_info"].get("first_name", "User"), r["total"]) for r in users_col.aggregate(pipeline)]

def get_top_by_games(period='week', limit=30):
    since = get_period_start(period)
    pipeline = [
        {"$match": {"time": {"$gte": since}}},
        {"$group": {"_id": "$user_id", "total": {"$sum": 1}}},
        {"$sort": {"total": -1}},
        {"$limit": limit},
        {"$lookup": {"from": "users", "localField": "_id", "foreignField": "user_id", "as": "user_info"}},
        {"$unwind": "$user_info"}
    ]
    return [(r["_id"], r["user_info"].get("first_name", "User"), r["total"]) for r in game_sessions_col.aggregate(pipeline)]

def get_top_by_wins(period='week', limit=30):
    since = get_period_start(period)
    pipeline = [
        {"$match": {"status": "Won", "time": {"$gte": since}}},
        {"$group": {"_id": "$user_id", "total": {"$sum": 1}}},
        {"$sort": {"total": -1}},
        {"$limit": limit},
        {"$lookup": {"from": "users", "localField": "_id", "foreignField": "user_id", "as": "user_info"}},
        {"$unwind": "$user_info"}
    ]
    return [(r["_id"], r["user_info"].get("first_name", "User"), r["total"]) for r in game_sessions_col.aggregate(pipeline)]

def get_user_rank(user_id, period='week', category='deposit'):
    if category == 'deposit': rows = get_top_by_deposit(period, 1000)
    elif category == 'invite': rows = get_top_by_invitations(period, 1000)
    elif category == 'wins': rows = get_top_by_wins(period, 1000)
    else: rows = get_top_by_games(period, 1000)
    
    for i, row in enumerate(rows):
        if row[0] == user_id: return i + 1, row[2]
    return None, 0


# ==========================================
# ADMIN: USER MANAGEMENT
# ==========================================

def get_all_users_with_stats(limit=500):
    users = list(users_col.find().sort("user_id", -1).limit(limit))
    result = []
    for u in users:
        result.append({
            'user_id': u.get("user_id"),
            'first_name': u.get("first_name", '—'),
            'phone': u.get("phone", '—'),
            'main_balance': u.get("main_balance", 0),
            'play_balance': u.get("play_balance", 0),
            'is_agent': u.get("is_agent", 0),
            'is_vip': u.get("is_vip", 0),
            'language': u.get("language", 'am'),
            'status': u.get("status", 'active'),
            'games_played': game_sessions_col.count_documents({"user_id": u["user_id"]}),
            'games_won': game_sessions_col.count_documents({"user_id": u["user_id"], "status": "Won"}),
            'referral_count': users_col.count_documents({"referred_by": u["user_id"]}),
        })
    return result

def ban_user(user_id):
    result = users_col.update_one({"user_id": user_id}, {"$set": {"status": "banned"}})
    return result.modified_count > 0

def unban_user(user_id):
    result = users_col.update_one({"user_id": user_id}, {"$set": {"status": "active"}})
    return result.modified_count > 0

def mark_vip(user_id, vip=True):
    result = users_col.update_one({"user_id": user_id}, {"$set": {"is_vip": 1 if vip else 0}})
    return result.modified_count > 0

def is_user_banned(user_id):
    u = users_col.find_one({"user_id": user_id})
    return u and u.get("status") == "banned"

def freeze_user(user_id):
    result = users_col.update_one({"user_id": user_id}, {"$set": {"status": "frozen"}})
    return result.modified_count > 0

def unfreeze_user(user_id):
    result = users_col.update_one({"user_id": user_id}, {"$set": {"status": "active"}})
    return result.modified_count > 0


# ==========================================
# ADMIN: DASHBOARD STATS
# ==========================================

def get_dashboard_stats():
    today = datetime.utcnow().strftime('%Y-%m-%d')
    total_users = users_col.count_documents({})
    
    def sum_transactions(tx_type, status='completed', date_prefix=None):
        match = {"type": tx_type, "status": status}
        if date_prefix: match["time"] = {"$regex": f"^{date_prefix}"}
        pipeline = [{"$match": match}, {"$group": {"_id": None, "total": {"$sum": "$amount"}}}]
        res = list(transactions_col.aggregate(pipeline))
        return res[0]["total"] if res else 0

    today_deposits = sum_transactions('deposit', 'completed', today)
    today_withdrawals = sum_transactions('withdraw', 'completed', today)
    today_payout = sum_transactions('bingo_win', 'completed', today)
    games_today = game_sessions_col.count_documents({"time": {"$regex": f"^{today}"}})

    return {
        'total_users': total_users,
        'today_deposits': today_deposits,
        'today_withdrawals': today_withdrawals,
        'today_profit': today_deposits - today_payout,
        'today_payout': today_payout,
        'games_today': games_today,
        'pending_deposits': transactions_col.count_documents({"type": "deposit", "status": "pending"}),
        'pending_withdrawals': transactions_col.count_documents({"type": "withdraw", "status": "pending"}),
    }


# ==========================================
# ADMIN: GAME HISTORY
# ==========================================

def get_admin_game_history(limit=100):
    pipeline = [
        {"$group": {
            "_id": "$game_id",
            "total_cards": {"$sum": 1},
            "total_income": {"$sum": "$entry_amount"},
            "payout": {"$sum": "$prize"},
            "winners": {"$sum": {"$cond": [{"$eq": ["$status", "Won"]}, 1, 0]}},
            "date": {"$max": "$time"}
        }},
        {"$sort": {"date": -1}},
        {"$limit": limit}
    ]
    games = []
    for r in game_sessions_col.aggregate(pipeline):
        cards = r["total_cards"] or 0
        pot = r["total_income"] or 0
        payout = r["payout"] or 0
        winners = r["winners"] or 0
        bet = round(pot / cards) if cards > 0 else 10
        total_payout = payout * winners if winners > 0 else payout
        games.append({
            'game_id': r["_id"], 'players': cards, 'bet': bet, 'winners': winners,
            'total_income': pot, 'payout': total_payout, 'profit': pot - total_payout, 'date': r["date"] or ''
        })
    return games


# ==========================================
# ADMIN: REPORTS
# ==========================================

def get_admin_reports(period='daily'):
    rows = []
    def get_stats_for_date(d_start, d_end=None):
        match_start = {"$regex": f"^{d_start}"}
        match_range = {"$gte": d_start, "$lt": d_end} if d_end else match_start
        
        match_dep = {"type": "deposit", "status": "completed", "time": match_range}
        match_wit = {"type": "withdraw", "status": "completed", "time": match_range}
        match_win = {"type": "bingo_win", "status": "completed", "time": match_range}
        
        def get_sum(match):
            p = [{"$match": match}, {"$group": {"_id": None, "t": {"$sum": "$amount"}}}]
            res = list(transactions_col.aggregate(p))
            return res[0]["t"] if res else 0

        dep = get_sum(match_dep)
        wit = get_sum(match_wit)
        pay = get_sum(match_win)
        games = game_sessions_col.count_documents({"time": match_range})
        return dep, wit, pay, games

    if period == 'daily':
        for i in range(7, -1, -1):
            d = (datetime.utcnow() - timedelta(days=i)).strftime('%Y-%m-%d')
            dep, wit, pay, games = get_stats_for_date(d)
            rows.append({'date': d, 'deposits': dep, 'withdrawals': wit, 'payout': pay, 'games': games, 'profit': dep - wit - pay})
    elif period == 'weekly':
        for i in range(3, -1, -1):
            ws = (datetime.utcnow() - timedelta(weeks=i))
            ws = ws - timedelta(days=ws.weekday())
            we = ws + timedelta(days=7)
            d_start = ws.strftime('%Y-%m-%d')
            d_end = we.strftime('%Y-%m-%d')
            dep, wit, pay, games = get_stats_for_date(d_start, d_end)
            rows.append({'date': f"{d_start} to {we.strftime('%Y-%m-%d')}", 'deposits': dep, 'withdrawals': wit, 'payout': pay, 'games': games, 'profit': dep - wit - pay})
    elif period == 'monthly':
        for i in range(5, -1, -1):
            dt = datetime.utcnow() - timedelta(days=i*30)
            m = dt.strftime('%Y-%m')
            dep, wit, pay, games = get_stats_for_date(m)
            rows.append({'date': m, 'deposits': dep, 'withdrawals': wit, 'payout': pay, 'games': games, 'profit': dep - wit - pay})
    return rows


# ==========================================
# AGENT SYSTEM FUNCTIONS
# ==========================================

def is_user_agent(user_id):
    u = users_col.find_one({"user_id": user_id})
    return u and u.get("is_agent") == 1

def check_and_upgrade_agent(user_id):
    if is_user_agent(user_id): return False
    invites = get_referral_count(user_id)
    depositors = get_depositing_referrals_count(user_id)
    total_deps = get_total_referral_deposits(user_id)
    if invites >= 30 and depositors >= 20 and total_deps >= 3000:
        users_col.update_one({"user_id": user_id}, {"$set": {"is_agent": 1}})
        return True
    return False


# ==========================================
# LANGUAGE FUNCTIONS
# ==========================================

def get_user_language(user_id):
    u = users_col.find_one({"user_id": user_id})
    return u.get("language", 'am') if u else 'am'

def set_user_language(user_id, lang):
    users_col.update_one({"user_id": user_id}, {"$set": {"language": lang}})


# ==========================================
# PHONE LOOKUP
# ==========================================

def get_user_by_phone(phone):
    clean = phone.replace(" ", "").replace("+", "").replace("-", "").replace("(", "").replace(")", "")
    variations = [clean]
    if clean.startswith("251"):
        variations.append("0" + clean[3:])
        variations.append("+251" + clean[3:])
    elif clean.startswith("0"):
        variations.append("251" + clean[1:])
        variations.append("+251" + clean[1:])
    elif len(clean) == 9 and not clean.startswith("0"):
        variations.append("0" + clean)
        variations.append("251" + clean)
        variations.append("+251" + clean)
    variations = list(set(variations))
    
    u = users_col.find_one({"phone": {"$in": variations}})
    if not u: return None
    return (u.get("user_id"), u.get("phone"), u.get("main_balance", 0), u.get("play_balance", 0), u.get("referred_by"))


# ==========================================
# MULTIPLAYER: GAMES (Step 6 upgrade)
# ----------------------------------------
# games:
#   game_id, stake, status, players_count, max_players,
#   called_numbers, current_number, prize_pool, winner,
#   created_at, started_at, finished_at
# game_players:
#   game_id, telegram_user_id, card_id, joined_at, status
# bingo_cards:
#   game_id, card_id, owner_id, numbers, marked_numbers
# game_history:
#   game_id, players, winner, prize, date
# ==========================================

def create_game_record(game_id, stake, status='waiting', max_players=100):
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    games_col.update_one(
        {"game_id": game_id},
        {"$setOnInsert": {
            "game_id": game_id, "stake": stake, "status": status,
            "players_count": 0, "max_players": max_players,
            "called_numbers": [], "current_number": None, "prize_pool": 0,
            "winner": None, "created_at": now, "started_at": None, "finished_at": None
        }},
        upsert=True
    )

def get_game_record(game_id):
    return games_col.find_one({"game_id": game_id})

def claim_open_room(stake, candidate_game_id):
    """
    Resolve the ONE open (waiting) room's game_id for a stake, atomically,
    across every server process/instance.

    If no room is open yet for this stake, `candidate_game_id` wins and
    becomes the open room. If another process already claimed one first,
    that existing game_id is returned instead and `candidate_game_id` is
    simply discarded. Mongo guarantees only one caller can ever "win" a
    given stake at a time, even with many processes calling this at once.

    This is what stops two different server processes from each silently
    handing out a different Game ID for the same stake tier.
    """
    doc = open_rooms_col.find_one_and_update(
        {"stake": stake},
        {"$setOnInsert": {"stake": stake, "game_id": candidate_game_id}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return doc["game_id"]

def release_open_room(stake, game_id):
    """Stop treating `game_id` as the open room for `stake` (only if it still is)."""
    open_rooms_col.delete_one({"stake": stake, "game_id": game_id})

def update_game_record(game_id, **fields):
    games_col.update_one({"game_id": game_id}, {"$set": fields})

def delete_game_record(game_id):
    games_col.delete_one({"game_id": game_id})

def add_game_player(game_id, user_id, card_id, stake):
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    game_players_col.update_one(
        {"game_id": game_id, "telegram_user_id": user_id},
        {"$setOnInsert": {
            "game_id": game_id, "telegram_user_id": user_id, "card_id": card_id,
            "joined_at": now, "status": "joined"
        }},
        upsert=True
    )
    games_col.update_one({"game_id": game_id}, {"$inc": {"players_count": 1}})

def update_game_player_status(game_id, user_id, status):
    game_players_col.update_one(
        {"game_id": game_id, "telegram_user_id": user_id},
        {"$set": {"status": status}}
    )

def get_game_players(game_id):
    return list(game_players_col.find({"game_id": game_id}))

def add_bingo_card(game_id, card_id, owner_id, numbers):
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    bingo_cards_col.insert_one({
        "game_id": game_id, "card_id": card_id, "owner_id": owner_id,
        "numbers": numbers, "marked_numbers": [], "created_at": now
    })

def get_bingo_card(card_id):
    return bingo_cards_col.find_one({"card_id": card_id})

def get_cards_for_player(game_id, user_id):
    return list(bingo_cards_col.find({"game_id": game_id, "owner_id": user_id}))

def update_card_marked(card_id, marked):
    bingo_cards_col.update_one({"card_id": card_id}, {"$set": {"marked_numbers": marked}})

def record_game_history(game_id, players, winners, prize, stake):
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    game_history_col.insert_one({
        "game_id": game_id, "players": players, "winner": winners,
        "prize": prize, "stake": stake, "date": now
    })

def get_recent_games(limit=50):
    return list(game_history_col.find().sort("date", -1).limit(limit))
