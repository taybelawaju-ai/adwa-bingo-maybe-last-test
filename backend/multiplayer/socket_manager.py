"""
socket_manager.py
-----------------
Real-time communication layer (Socket.IO).

Client -> Server events:
    join_game      {stake?, game_id?, user_id, name}
    select_card    {game_id, user_id, label?}
    deselect_card  {game_id, user_id, card_id}
    mark_number    {game_id, user_id, card_id, number}
    claim_bingo    {game_id, user_id, card_id, name}
    leave_game     {game_id, user_id}

Server -> Client events (to the game room "game:{game_id}"):
    game_joined          (personal snapshot after join)
    player_joined        {game_id, name, players, cards}
    player_count_update  {game_id, players, max_players, cards}
    game_started         {game_id, stake, players, cards, prize_pool, ...}
    timer_update         {game_id, time_left, status}
    number_called        {game_id, number, called_count, current_number}
    card_update          (personal: card assigned / removed / marked / error)
    claim_rejected       (personal: {error})
    winner_found         {game_id, winner_name, prize_per_winner, ...}
    game_finished        {game_id, status, winners, prize_pool, ...}

Legacy events from the old system are still emitted so the existing admin
panel and old clients keep working:
    join_room / leave_room / request_countdown / player_ready /
    declare_winner / admin_manual_call / set_max_winners /
    admin_pause_game / admin_cancel_game
    -> game_state_update, countdown_update, player_joined, game_started,
       ball_called, winner_found, max_winners_updated, game_paused,
       game_cancelled, game_ended
"""
from flask import request
from flask_socketio import join_room, leave_room

from backend.multiplayer.room_manager import Room

GAME_ROOM_PREFIX = 'game:'
DISCONNECT_GRACE = 45  # seconds a dropped socket may still reconnect before purge


class SocketManager:
    """Wires every Socket.IO handler to the room manager + game engine."""

    def __init__(self, socketio, manager, engine):
        self.socketio = socketio
        self.manager = manager
        self.engine = engine
        # engine broadcasts are delivered through this manager
        self.engine.broadcast = self.emit_game
        self.engine.broadcast_legacy = self.emit_game_legacy
        self.engine.purge_disconnected = self._purge_disconnected
        self.sid_rooms = {}                # sid -> [game_id]
        self.register()

    # ------------------------------------------------------- broadcasting
    def emit_game(self, game_id, event, data):
        """Emit an event to every socket inside a game room."""
        self.socketio.emit(event, data, room=f"{GAME_ROOM_PREFIX}{game_id}")

    def emit_game_legacy(self, game_id, event, data):
        """Legacy events also reach the old bingo_room_{stake} rooms."""
        self.emit_game(game_id, event, data)
        room = data.get('room')
        if room:
            self.socketio.emit(event, data, room=f"bingo_room_{room}")

    def emit_personal(self, event, data, sid=None):
        self.socketio.emit(event, data, to=sid or request.sid)

    def _stake_from(self, data, default=10):
        try:
            return int(data.get('stake') or data.get('room') or default)
        except (TypeError, ValueError):
            return default

    def _user_from(self, data):
        try:
            return int(data.get('user_id'))
        except (TypeError, ValueError):
            return None

    # ----------------------------------------------------------- registration
    def register(self):
        sio = self.socketio

        @sio.on('connect')
        def on_connect():
            print(f"🔌 Client connected: {request.sid}")

        @sio.on('disconnect')
        def on_disconnect():
            sid = request.sid
            print(f"🔌 Client disconnected: {sid}")
            # mark every room player bound to this socket as offline; the
            # engine purges them after DISCONNECT_GRACE (refund if waiting).
            for game_id in list(self.manager.rooms.keys()):
                room = self.manager.get_room(game_id)
                if not room:
                    continue
                for uid, p in list(room.players.items()):
                    if p.sid == sid:
                        self.manager.mark_disconnected(game_id, uid)

        @sio.on('join_game')
        def on_join_game(data):
            self.handle_join_game(data or {})

        @sio.on('select_card')
        def on_select_card(data):
            self.handle_select_card(data or {})

        @sio.on('deselect_card')
        def on_deselect_card(data):
            self.handle_deselect_card(data or {})

        @sio.on('mark_number')
        def on_mark_number(data):
            self.handle_mark_number(data or {})

        @sio.on('claim_bingo')
        def on_claim_bingo(data):
            self.handle_claim_bingo(data or {})

        @sio.on('leave_game')
        def on_leave_game(data):
            self.handle_leave_game(data or {})

        # ---------------- legacy (old frontend / admin tools) -------------
        @sio.on('join_room')
        def on_join_room(data):
            self.handle_legacy_join_room(data or {})

        @sio.on('leave_room')
        def on_leave_room(data):
            room = self._stake_from(data or {})
            leave_room(f"bingo_room_{room}")

        @sio.on('request_countdown')
        def on_request_countdown(data):
            self.handle_legacy_countdown(data or {})

        @sio.on('player_ready')
        def on_player_ready(data):
            self.handle_legacy_player_ready(data or {})

        @sio.on('ready_to_play')
        def on_ready_to_play(data):
            self.handle_ready_to_play(data or {})

        @sio.on('declare_winner')
        def on_declare_winner(data):
            self.handle_legacy_declare_winner(data or {})

        @sio.on('admin_manual_call')
        def on_admin_manual_call(data):
            self.handle_admin_manual_call(data or {})

        @sio.on('set_max_winners')
        def on_set_max_winners(data):
            self.handle_admin_max_winners(data or {})

        @sio.on('admin_pause_game')
        def on_admin_pause_game(data):
            self.handle_admin_pause(data or {})

        @sio.on('admin_cancel_game')
        def on_admin_cancel_game(data):
            self.handle_admin_cancel(data or {})

    # ----------------------------------------------------------- new events
    def handle_join_game(self, data):
        stake = self._stake_from(data)
        user_id = self._user_from(data)
        name = data.get('name') or 'Player'
        explicit = data.get('game_id')

        if user_id is None or user_id <= 0:
            # no valid Telegram identity -> never register a phantom player
            # or let it count toward the minimum player count
            self.emit_personal('game_joined_error', {'error': 'invalid_user'})
            return

        if not self.manager.db.user_exists(user_id):
            try:
                self.manager.db.add_user(user_id, first_name=name)
                print(f"[socket_manager] auto-registered {user_id}")
            except Exception as e:
                print(f"[socket_manager] auto-register error: {e}")

        if explicit:
            room = self.manager.get_room(explicit)
            if not room:
                self.emit_personal('game_joined_error', {'error': 'room_not_found'})
                return
            game_id = explicit
        else:
            room, created = self.manager.get_or_create_open_room(stake, user_id)
            game_id = room.game_id

        room, err = self.manager.join(game_id, user_id, name, request.sid)
        if err:
            self.emit_personal('game_joined_error', {'error': err})
            return

        join_room(f"{GAME_ROOM_PREFIX}{game_id}")
        self.engine.ensure_loop(game_id)

        payload = {
            'game_id': game_id,
            'stake': room.stake,
            'status': room.status,
            'time_left': room.time_left(),
            'players': self._count_for(room),
            'max_players': room.max_players,
            'cards': room.total_cards(),
            'prize_pool': room.prize_pool(),
            'max_winners': room.max_winners,
            'cards_limit': 2,
            'called_numbers': list(room.called),
            'current_number': room.current_number,
            'my_cards': room.my_cards(user_id),
            'ready': room.ready_phase,
            'roster': self._players_payload(room)['players'],
            'taken_labels': sorted(room.used_labels),
            'main_balance': self._main_balance(user_id),
            'play_balance': self._play_balance(user_id),
        }
        self.emit_personal('game_joined', payload)

        roster = self._players_payload(room)
        self.emit_game(game_id, 'player_joined', {
            'game_id': game_id,
            'name': name,
            'players': self._count_for(room),
            'cards': roster['cards'],
            'roster': roster['players'],
            'taken_labels': roster['taken_labels'],
        })
        self.emit_game(game_id, 'player_count_update', {
            'game_id': game_id,
            'players': self._count_for(room),
            'max_players': room.max_players,
            'cards': roster['cards'],
        })
        self.broadcast_roster(game_id)
        self.emit_game_legacy(game_id, 'player_joined', {
            'room': room.stake,
            'total_players': room.total_cards(),
            'player_name': name,
        })

    def handle_select_card(self, data):
        game_id = data.get('game_id')
        user_id = self._user_from(data)
        label = data.get('label')
        if not game_id or user_id is None:
            self.emit_personal('card_update', {'error': 'missing_params'})
            return
        room = self.manager.get_room(game_id)
        if not room:
            self.emit_personal('card_update', {'error': 'room_not_found'})
            return
        card, err = self.manager.assign_card(game_id, user_id, label=label)
        if err:
            self.emit_personal('card_update', {'error': err, 'game_id': game_id})
            return
        # charge the stake server-side (never trust the frontend to pay).
        # Every card costs one stake: first card = stake, second card = stake again.
        charged = self.manager.db.deduct_bet_smart(user_id, room.stake)
        if not charged:
            self.manager.release_card(game_id, user_id, card['card_id'])
            self.emit_personal('card_update', {
                'error': 'insufficient_balance',
                'game_id': game_id,
                'main_balance': self._main_balance(user_id),
                'play_balance': self._play_balance(user_id),
            })
            return
        try:
            self.manager.db.add_transaction(user_id, 'bingo_bet', room.stake)
        except Exception as e:
            print(f"[socket_manager] db add_transaction error: {e}")
        self.emit_personal('card_update', {
            'type': 'assigned',
            'game_id': game_id,
            'card_id': card['card_id'],
            'label': card['label'],
            'numbers': card['numbers'],
            'marked': [],
            'cards_count': len(room.players.get(user_id).cards),
            'main_balance': self._main_balance(user_id),
            'play_balance': self._play_balance(user_id),
        })
        self.emit_game(game_id, 'player_count_update', {
            'game_id': game_id,
            'players': self._count_for(room),
            'max_players': room.max_players,
            'cards': room.total_cards(),
        })
        self.broadcast_roster(game_id)

    def handle_deselect_card(self, data):
        game_id = data.get('game_id')
        user_id = self._user_from(data)
        card_id = data.get('card_id')
        room = self.manager.get_room(game_id)
        if not room:
            self.emit_personal('card_update', {'type': 'removed', 'error': 'room_not_found', 'game_id': game_id})
            return
        refund, card, err = self.manager.release_card(game_id, user_id, card_id)
        if err:
            self.emit_personal('card_update', {'type': 'removed', 'error': err, 'game_id': game_id})
            return
        # refund the stake for EVERY released card (each card costs one stake)
        if refund:
            try:
                self.manager.db.update_play_balance(user_id, refund)
                self.manager.db.add_transaction(user_id, 'bingo_refund', refund)
            except Exception as e:
                print(f"[socket_manager] db refund error: {e}")
        self.emit_personal('card_update', {
            'type': 'removed',
            'game_id': game_id,
            'card_id': card_id,
            'refund': refund,
            'main_balance': self._main_balance(user_id),
            'play_balance': self._play_balance(user_id),
        })
        self.emit_game(game_id, 'player_count_update', {
            'game_id': game_id,
            'players': self._count_for(room),
            'max_players': room.max_players,
            'cards': room.total_cards(),
        })
        self.broadcast_roster(game_id)

    def handle_mark_number(self, data):
        game_id = data.get('game_id')
        user_id = self._user_from(data)
        card_id = data.get('card_id')
        number = data.get('number')
        if not game_id or user_id is None or not card_id or not isinstance(number, int):
            self.emit_personal('card_update', {'error': 'missing_params'})
            return
        room = self.manager.get_room(game_id)
        if not room:
            self.emit_personal('card_update', {'error': 'room_not_found'})
            return
        with room.lock:
            if number not in room.called:
                self.emit_personal('card_update', {'type': 'marked', 'error': 'not_called', 'card_id': card_id})
                return
            card, _ = self.manager.find_card(game_id, user_id, card_id)
            if not card:
                self.emit_personal('card_update', {'error': 'card_not_found'})
                return
            if number in card['numbers']:
                call_col = (number - 1) // 15
                for idx, v in enumerate(card['numbers']):
                    if v == number and idx % 5 == call_col:
                        card['marked'].add(idx)
            try:
                self.manager.db.update_card_marked(card_id, sorted(card['marked']))
            except Exception as e:
                print(f"[socket_manager] db update_card_marked error: {e}")
            self.emit_personal('card_update', {
                'type': 'marked',
                'game_id': game_id,
                'card_id': card_id,
                'marked': sorted(card['marked']),
            })

    def handle_claim_bingo(self, data):
        game_id = data.get('game_id')
        user_id = self._user_from(data)
        card_id = data.get('card_id')
        name = data.get('name')
        result = self.engine.claim_bingo(game_id, user_id, card_id, name=name)
        if not result['ok']:
            self.emit_personal('claim_rejected', {
                'game_id': game_id,
                'error': result['error'],
            })

    def handle_leave_game(self, data):
        game_id = data.get('game_id')
        user_id = self._user_from(data)
        if not game_id or user_id is None:
            return
        refund, room = self.manager.leave(game_id, user_id)
        if refund:
            try:
                self.manager.db.update_play_balance(user_id, refund)
                self.manager.db.add_transaction(user_id, 'bingo_refund', refund)
            except Exception as e:
                print(f"[socket_manager] db leave refund error: {e}")
        leave_room(f"{GAME_ROOM_PREFIX}{game_id}")
        self.emit_personal('left_game', {
            'game_id': game_id,
            'refund': refund,
            'main_balance': self._main_balance(user_id),
            'play_balance': self._play_balance(user_id),
        })
        if room:
            self.emit_game(game_id, 'player_count_update', {
                'game_id': game_id,
                'players': self._count_for(room),
                'max_players': room.max_players,
                'cards': room.total_cards(),
            })
            self.broadcast_roster(game_id)

    # ------------------------------------------------- legacy event handlers
    def handle_legacy_join_room(self, data):
        """Old frontend: join_room {room:'10'} -> join the open room."""
        stake = self._stake_from(data)
        room = self.manager.get_open_room(stake)
        if not room:
            # never create a room with 0 players from a legacy event
            return
        join_room(f"bingo_room_{stake}")
        join_room(f"{GAME_ROOM_PREFIX}{room.game_id}")
        self.engine.ensure_loop(room.game_id)
        time_left = room.time_left()
        self.emit_game_legacy(room.game_id, 'countdown_update', {
            'room': stake,
            'game_id': room.game_id,
            'time_left': time_left,
        })

    def handle_legacy_countdown(self, data):
        stake = self._stake_from(data)
        room = self.manager.get_open_room(stake)
        if not room:
            # never create a room with 0 players from a legacy event
            return
        self.engine.ensure_loop(room.game_id)  # sets room.deadline when missing
        self.emit_game_legacy(room.game_id, 'countdown_update', {
            'room': stake,
            'game_id': room.game_id,
            'time_left': room.time_left(),
        })

    def handle_ready_to_play(self, data):
        """Player pressed GET READY: allow the first ball to be called."""
        game_id = data.get('game_id')
        user_id = self._user_from(data)
        room = self.manager.get_room(game_id)
        if not room:
            return
        with room.lock:
            room.ready_players.add(user_id)

    def handle_legacy_player_ready(self, data):
        """Old frontend: player_ready {user_id, name, cards:[labels]}."""
        stake = self._stake_from(data)
        user_id = self._user_from(data)
        name = data.get('name', 'Player')
        room, _ = self.manager.get_or_create_open_room(stake)
        game_id = room.game_id
        room, err = self.manager.join(game_id, user_id, name, request.sid)
        if err:
            return
        join_room(f"{GAME_ROOM_PREFIX}{game_id}")
        join_room(f"bingo_room_{stake}")
        for label in data.get('cards', [])[:2]:
            card, cerr = self.manager.assign_card(game_id, user_id, label=label)
            if cerr:
                card, cerr = self.manager.assign_card(game_id, user_id, label=None)
                if cerr:
                    continue
        self.engine.ensure_loop(game_id)
        self.emit_game_legacy(game_id, 'player_joined', {
            'room': stake,
            'total_players': room.total_cards(),
            'player_name': name,
        })
        self.broadcast_roster(game_id)

    def handle_legacy_declare_winner(self, data):
        """Old frontend: declare_winner -> server-side validated claim."""
        stake = self._stake_from(data)
        user_id = self._user_from(data)
        name = data.get('name', 'Player')
        game_id = data.get('game_id')
        room = self.manager.get_open_room(stake) or self.manager.get_room(game_id)
        if not room:
            return
        card = None
        p = room.players.get(user_id)
        if p and p.cards:
            card = p.cards[0]['card_id']
        if card:
            self.engine.claim_bingo(room.game_id, user_id, card, name=name)

    def handle_admin_manual_call(self, data):
        stake = self._stake_from(data)
        number = data.get('number')
        room = self.manager.get_open_room(stake) or self.manager.get_room(data.get('game_id'))
        if not room:
            return
        self.engine.call_specific(room.game_id, number, manual=True)

    def handle_admin_max_winners(self, data):
        stake = self._stake_from(data)
        room = self.manager.get_open_room(stake) or self.manager.get_room(data.get('game_id'))
        if not room:
            return
        self.engine.set_max_winners(room.game_id, data.get('max', 1))

    def handle_admin_pause(self, data):
        stake = self._stake_from(data)
        room = self.manager.get_open_room(stake) or self.manager.get_room(data.get('game_id'))
        if not room:
            return
        self.engine.pause_resume(room.game_id)

    def handle_admin_cancel(self, data):
        stake = self._stake_from(data)
        room = self.manager.get_open_room(stake) or self.manager.get_room(data.get('game_id'))
        if not room:
            return
        self.engine.cancel_game(room.game_id)

    # ------------------------------------------------------------ db helpers
    def _count_for(self, room):
        """Lobby count in WAITING (connected users only, ghosts excluded);
        number of cards chosen once running."""
        if room.status == Room.RUNNING:
            return room.total_cards()
        return room.online_count()

    # ------------------------------------------------------------ roster helper
    def _players_payload(self, room):
        """Full lobby roster: every player with the card labels they took."""
        players = []
        for uid in sorted(room.players):
            p = room.players[uid]
            players.append({
                'user_id': p.user_id,
                'name': p.name,
                'cards': [c['label'] for c in p.cards],
            })
        return {
            'game_id': room.game_id,
            'players': players,
            'player_count': self._count_for(room),
            'cards': room.total_cards(),
            'taken_labels': sorted(room.used_labels),
        }

    def broadcast_roster(self, game_id):
        room = self.manager.get_room(game_id)
        if not room:
            return
        self.emit_game(game_id, 'players_update', self._players_payload(room))

    def _purge_disconnected(self, game_id):
        """Engine callback: drop ghosts after grace, refund, refresh the lobby."""
        removed = self.manager.purge_disconnected(game_id, grace=DISCONNECT_GRACE)
        if not removed:
            return
        for uid, refund in removed:
            if refund:
                try:
                    self.manager.db.update_play_balance(uid, refund)
                    self.manager.db.add_transaction(uid, 'bingo_refund', refund)
                except Exception as e:
                    print(f"[socket_manager] purge refund error: {e}")
        room = self.manager.get_room(game_id)
        if room:
            self.emit_game(game_id, 'player_count_update', {
                'game_id': game_id,
                'players': self._count_for(room),
                'max_players': room.max_players,
                'cards': room.total_cards(),
            })
            self.broadcast_roster(game_id)

    # ------------------------------------------------------------ db helpers
    def _main_balance(self, user_id):
        try:
            return self.manager.db.get_main_balance(user_id)
        except Exception:
            return 0

    def _play_balance(self, user_id):
        try:
            return self.manager.db.get_play_balance(user_id)
        except Exception:
            return 0
