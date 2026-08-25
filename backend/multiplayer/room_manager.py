"""
room_manager.py
---------------
Creates, joins and manages unlimited multiplayer Bingo rooms.

Each room is a dynamic object (NOT fixed "Game 10 / Game 20" states):

    {
        game_id,        # unique 8-char code e.g. "BB3XOJPN"
        stake,          # birr per card
        status,         # waiting | running | finished | cancelled
        players,        # { user_id: Player }
        cards,          # per-player card dicts {card_id, label, numbers, marked}
        called,         # ordered list of numbers already called
        current_number, # last called number
        timer,          # countdown deadline (epoch seconds)
        winners,        # validated winners
        prize_pool      # total stakes collected (cards * stake)
    }

Every stake has a current "open" waiting room. Players who do not pass a
specific game_id are placed into the open room of their stake. Unlimited
rooms are supported (new game_id every round).
"""
import os
import random
import string
import threading
import time

import db as db_module

# ---------------------------------------------------------------- constants
MAX_CARDS_PER_PLAYER = 2      # keep original UI limit (2 cards)
MAX_CARD_LABELS = 400         # card labels 1..400 like the original UI
DEFAULT_MAX_PLAYERS = 500     # room occupancy cap - comfortably above the
                               # 400-card pool so card-holders are never the
                               # bottleneck; spectators don't consume a card
                               # slot so they can push past 400 too
GAME_ID_ALPHA = string.ascii_uppercase
GAME_ID_CHARS = string.ascii_uppercase + string.digits


def generate_game_id():
    """8-character game code like 'BB3XOJPN' (2 letters + 6 alphanumerics)."""
    code = ''.join(random.choice(GAME_ID_ALPHA) for _ in range(2))
    code += ''.join(random.choice(GAME_ID_CHARS) for _ in range(6))
    return code


class Player:
    """A player currently present in a room."""

    def __init__(self, user_id, name='Player', sid=None):
        self.user_id = user_id
        self.name = name or 'Player'
        self.sid = sid                       # socket session id (optional)
        self.cards = []                      # [{card_id, label, numbers, marked}]
        self.joined_at = time.time()
        self.disconnected_at = None          # epoch when the socket dropped (None = online)

    def card_count(self):
        return len(self.cards)


class Room:
    """One multiplayer game room."""

    WAITING = 'waiting'
    RUNNING = 'running'
    FINISHED = 'finished'
    CANCELLED = 'cancelled'

    def __init__(self, game_id, stake, max_players=DEFAULT_MAX_PLAYERS, countdown=35):
        self.game_id = game_id
        self.stake = stake
        self.max_players = max_players
        self.countdown = countdown
        self.status = Room.WAITING
        self.players = {}                    # user_id -> Player
        self.called = []                     # called numbers (ordered)
        self.current_number = None
        self.deadline = None                 # countdown deadline (epoch)
        self.started_at = None
        self.finished_at = None
        self.winners = []                    # [{user_id,name,card_id,card_label,lines,at}]
        self.max_winners = int(os.environ.get("DEFAULT_MAX_WINNERS", "3") or "3")
        self.paused = False
        self.ready_phase = False          # GET READY gate: no balls until players are ready
        self.ready_players = set()        # users who pressed GET READY
        self.created_at = time.time()
        self.used_labels = set()             # card labels already taken
        self.layouts = set()                 # unique card layouts in this room
        self.lock = threading.RLock()

    # ------------------------------------------------------------- helpers
    def total_cards(self):
        """Total number of paid card slots in the room."""
        return sum(p.card_count() for p in self.players.values())

    def player_count(self):
        return len(self.players)

    def playing_count(self):
        """Players who actually hold at least one card (real participants)."""
        return sum(1 for p in self.players.values() if p.cards)

    def online_count(self):
        """Players whose socket is still connected."""
        return sum(1 for p in self.players.values() if p.disconnected_at is None)

    def prize_pool(self):
        """All stakes collected so far."""
        return self.total_cards() * self.stake

    def time_left(self):
        """Seconds remaining in the waiting countdown."""
        if self.deadline is None:
            return self.countdown
        return max(0, int(self.deadline - time.time()))

    def my_cards(self, user_id):
        """Serialize the cards of one player (safe payload for that player only)."""
        p = self.players.get(user_id)
        if not p:
            return []
        return [{
            'card_id': c['card_id'],
            'label': c['label'],
            'numbers': c['numbers'],
            'marked': sorted(c['marked']),
        } for c in p.cards]


class RoomManager:
    """Owns every room, the open-room-per-stake map and all card logic."""

    def __init__(self, db=None):
        self.db = db_module if db is None else db
        self.rooms = {}                      # game_id -> Room
        self.open_by_stake = {}              # stake -> game_id (current open room)
        self.lock = threading.RLock()

    # ------------------------------------------------------------ creation
    def create_room(self, stake, game_id=None, max_players=DEFAULT_MAX_PLAYERS):
        """
        Create a fresh room and make it the open room for its stake.

        When `game_id` isn't given explicitly, the open-room slot is claimed
        atomically through MongoDB (db.claim_open_room) instead of being
        decided purely from this process's own memory. If this is the only
        server process running, that claim always succeeds with our own
        candidate id, so behaviour is unchanged. If more than one process is
        serving traffic (multiple Render instances/workers), whichever
        process gets there first wins the game_id, and every other process
        adopts that SAME id instead of minting its own - closing the "each
        user sees a different Game ID" gap.

        Note: this makes the game_id itself consistent across processes. It
        does not, by itself, make one process aware of players who joined
        through a different process - that requires a shared Socket.IO
        message queue (e.g. Redis) too. See PHASE_REPORT.md / the chat
        summary for why running a single process is still the fastest full
        fix if you're not already set up for that.
        """
        with self.lock:
            explicit = game_id is not None
            if not explicit:
                for _ in range(100):         # avoid local game_id collisions
                    game_id = generate_game_id()
                    if game_id not in self.rooms:
                        break
                try:
                    game_id = self.db.claim_open_room(stake, game_id)
                except Exception as e:
                    print(f"[room_manager] claim_open_room error, using local id only: {e}")

            room = self.rooms.get(game_id)
            if room is None:
                room = Room(game_id, stake, max_players)
                self.rooms[game_id] = room
                try:
                    self.db.create_game_record(game_id, stake, status=Room.WAITING, max_players=max_players)
                except Exception as e:
                    print(f"[room_manager] db create_game_record error: {e}")
            self.open_by_stake[stake] = game_id
            return room

    def get_room(self, game_id):
        return self.rooms.get(game_id)

    def get_open_room(self, stake):
        """Current open (waiting) room for a stake, if any."""
        with self.lock:
            gid = self.open_by_stake.get(stake)
            room = self.rooms.get(gid) if gid else None
            if room and room.status == Room.WAITING:
                return room
            return None

    def get_or_create_open_room(self, stake, user_id=None):
        """
        Reuse the open room for a stake, or join the currently running game of
        the same stake as an observer, or create/adopt a brand-new room.

        A player who already sits in a running game of this stake always goes
        back to THAT game (their cards are still there) - never a new id.

        `created` is True only when THIS call is the one that actually won
        the Mongo claim for a brand-new room - if another process already
        had one open, we adopt its game_id and `created` comes back False,
        even though this process still had to build a local Room object to
        track the players who connect to it here.
        """
        with self.lock:
            # 1) resume the running game the user is already playing
            if user_id:
                mine = [r for r in self.rooms.values()
                        if r.stake == stake and r.status == Room.RUNNING
                        and user_id in r.players]
                if mine:
                    return max(mine, key=lambda r: r.started_at or 0), False

            # 2) the open waiting room of this stake
            room = self.get_open_room(stake)
            if room:
                return room, False

            # 3) any running game of this stake with CONNECTED players still in it
            #    (join as observer / new player). A running game with 0
            #    connected players is a ghost and must not swallow new
            #    joiners - they get a fresh waiting room instead.
            running = [r for r in self.rooms.values()
                       if r.stake == stake and r.status == Room.RUNNING
                       and r.online_count() > 0]
            if running:
                return max(running, key=lambda r: r.started_at or 0), False

            candidate = generate_game_id()
            while candidate in self.rooms:
                candidate = generate_game_id()
            try:
                authoritative_id = self.db.claim_open_room(stake, candidate)
            except Exception as e:
                print(f"[room_manager] claim_open_room error, using local id only: {e}")
                authoritative_id = candidate

            room = self.create_room(stake, game_id=authoritative_id)
            return room, authoritative_id == candidate

    def reset_open(self, room):
        """Unregister a room as 'open' (used when the game starts/finishes)."""
        with self.lock:
            if self.open_by_stake.get(room.stake) == room.game_id:
                del self.open_by_stake[room.stake]
        try:
            self.db.release_open_room(room.stake, room.game_id)
        except Exception as e:
            print(f"[room_manager] release_open_room error: {e}")

    # --------------------------------------------------------------- join
    def join(self, game_id, user_id, name, sid=None):
        """
        Add a player to a room. Returns (room, error). Already-joined players
        just refresh their socket sid. Observers may join running games.
        """
        room = self.rooms.get(game_id)
        if not room:
            return None, 'room_not_found'
        if user_id is None or user_id <= 0:
            return None, 'invalid_user'
        with room.lock:
            if user_id in room.players:          # re-join (reconnect)
                room.players[user_id].sid = sid
                room.players[user_id].disconnected_at = None
                return room, None
            if room.status in (Room.FINISHED, Room.CANCELLED):
                return room, 'game_finished'
            if room.player_count() >= room.max_players:
                return room, 'room_full'
            p = Player(user_id, name, sid)
            room.players[user_id] = p
            try:
                self.db.add_game_player(game_id, user_id, '', room.stake)
            except Exception as e:
                print(f"[room_manager] db add_game_player error: {e}")
            return room, None

    def leave(self, game_id, user_id, running_refund=False):
        """
        Remove a player. Refund is returned only when the game has not
        started yet (leaving mid-game forfeits the stake, like real bingo).
        Returns (refund_amount, room).
        """
        room = self.rooms.get(game_id)
        if not room:
            return None, 'room_not_found'
        if user_id is None or user_id <= 0:
            return None, 'invalid_user'
        with room.lock:
            p = room.players.pop(user_id, None)
            if not p:
                return 0, room
            refund = 0
            if p.cards:
                for c in p.cards:
                    room.used_labels.discard(c['label'])
            if room.status == Room.WAITING and p.cards:
                refund = len(p.cards) * room.stake
            try:
                self.db.update_game_player_status(game_id, user_id, 'left')
            except Exception as e:
                print(f"[room_manager] db update_game_player_status error: {e}")
            return refund, room

    # ------------------------------------------------------------ presence
    def mark_disconnected(self, game_id, user_id):
        """Socket dropped: mark the player offline (grace period before purge)."""
        room = self.rooms.get(game_id)
        if not room:
            return
        with room.lock:
            p = room.players.get(user_id)
            if p and p.disconnected_at is None:
                p.disconnected_at = time.time()

    def purge_disconnected(self, game_id, grace=60):
        """
        Remove players whose socket has been gone for longer than `grace`
        seconds. In a WAITING game their card stakes are refunded; in a
        RUNNING game card-holders stay in (their cards can still win) and
        only observers are removed. Returns [(user_id, refund_amount)].
        """
        room = self.rooms.get(game_id)
        if not room:
            return []
        removed = []
        with room.lock:
            now = time.time()
            for uid in list(room.players.keys()):
                p = room.players[uid]
                if p.disconnected_at is None:
                    continue
                if now - p.disconnected_at < grace:
                    continue
                if room.status == Room.RUNNING and p.cards:
                    continue                     # card-holder may still win
                refund = len(p.cards) * room.stake if (room.status == Room.WAITING and p.cards) else 0
                for c in p.cards:
                    room.used_labels.discard(c['label'])
                del room.players[uid]
                try:
                    self.db.update_game_player_status(game_id, uid, 'left')
                except Exception as e:
                    print(f"[room_manager] purge status error: {e}")
                removed.append((uid, refund))
        return removed

    # --------------------------------------------------------------- cards
    def assign_card(self, game_id, user_id, label=None):
        """
        Assign a unique server-generated card to a player.
        Card labels (1..400) are unique per room and exact layouts never
        repeat inside one game. Returns (card_dict, error).
        """
        room = self.rooms.get(game_id)
        if not room:
            return None, 'room_not_found'
        with room.lock:
            if room.status != Room.WAITING:
                return None, 'game_already_started'
            p = room.players.get(user_id)
            if not p:
                return None, 'not_in_game'
            if len(p.cards) >= MAX_CARDS_PER_PLAYER:
                return None, 'max_cards_reached'
            if label is not None:
                if label in room.used_labels:
                    return None, 'card_taken'
            else:
                label = self._free_label(room)
                if label is None:
                    return None, 'no_cards_left'
            numbers = self._unique_layout(room)
            card_id = f"{game_id}-{label}"
            p.cards.append({
                'card_id': card_id,
                'label': label,
                'numbers': numbers,
                'marked': set(),
            })
            room.used_labels.add(label)
            try:
                self.db.add_bingo_card(game_id, card_id, user_id, numbers)
                self.db.update_game_player_status(game_id, user_id, 'playing')
            except Exception as e:
                print(f"[room_manager] db add_bingo_card error: {e}")
            return p.cards[-1], None

    def release_card(self, game_id, user_id, card_id):
        """Remove a card and return its stake (only while waiting)."""
        room = self.rooms.get(game_id)
        if not room:
            return 0, None, 'room_not_found'
        with room.lock:
            if room.status != Room.WAITING:
                return 0, None, 'game_already_started'
            p = room.players.get(user_id)
            if not p:
                return 0, None, 'not_in_game'
            for i, c in enumerate(p.cards):
                if c['card_id'] == card_id:
                    del p.cards[i]
                    room.used_labels.discard(c['label'])
                    return room.stake, c, None
            return 0, None, 'card_not_found'

    def find_card(self, game_id, user_id, card_id):
        """Locate a card owned by a player (None when missing)."""
        room = self.rooms.get(game_id)
        if not room:
            return None, None
        p = room.players.get(user_id)
        if not p:
            return None, None
        for c in p.cards:
            if c['card_id'] == card_id:
                return c, room
        return None, room

    def _free_label(self, room):
        """Pick an unused card label between 1 and 400."""
        free = [n for n in range(1, MAX_CARD_LABELS + 1) if n not in room.used_labels]
        return random.choice(free) if free else None

    def _unique_layout(self, room):
        """Generate a card layout that does not repeat inside this room."""
        from backend.multiplayer.bingo_validator import generate_card_numbers
        for _ in range(30):
            numbers = generate_card_numbers()
            key = tuple(numbers)
            if key not in room.layouts:
                room.layouts.add(key)
                return numbers
        return generate_card_numbers()       # accept duplicate after retries

    # -------------------------------------------------------------- stats
    def active_cards_total(self):
        """Total paid card slots across all rooms (used by admin dashboard)."""
        return sum(r.total_cards() for r in self.rooms.values())

    def running_games_count(self):
        return sum(1 for r in self.rooms.values() if r.status == Room.RUNNING)

    def all_rooms_snapshot(self):
        return {gid: room for gid, room in self.rooms.items()}

    # ------------------------------------------------------------- cleanup
    def cleanup_rooms(self, max_age=600):
        """Remove finished/cancelled rooms after 60s and abandoned lobbies."""
        with self.lock:
            now = time.time()
            for gid in list(self.rooms.keys()):
                room = self.rooms[gid]
                if room.status in (Room.FINISHED, Room.CANCELLED):
                    if room.finished_at and now - room.finished_at > 60:
                        del self.rooms[gid]
                        self._clear_open_ref(room)
                        try:
                            self.db.delete_game_record(gid)
                        except Exception as e:
                            print(f"[room_manager] db delete_game_record error: {e}")
                elif (room.status == Room.WAITING and not room.players
                      and now - room.created_at > max_age):
                    del self.rooms[gid]
                    self._clear_open_ref(room)

    def _clear_open_ref(self, room):
        if self.open_by_stake.get(room.stake) == room.game_id:
            del self.open_by_stake[room.stake]
