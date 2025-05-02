import logging
import os
import sqlite3
import time
import json
import asyncio
import random
from datetime import datetime
from enum import Enum, auto
from typing import Dict, List, Set, Tuple, Optional, Union, Any

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

# Configuration from environment variables
BOT_TOKEN = os.environ.get('BOT_TOKEN', '')
DATABASE_PATH = os.environ.get('DATABASE_PATH', 'database.db')
CRYPTO_BOT_API_KEY = os.environ.get('CRYPTO_BOT_API_KEY', '')
CRYPTO_BOT_API_URL = "https://pay.crypt.bot/api/"
ADMIN_IDS = [int(id_) for id_ in os.environ.get('ADMIN_IDS', '').split(',') if id_]
BOT_USERNAME = os.environ.get('BOT_USERNAME', '')

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler("seka-bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Emoji constants
EMOJI = {
    "hearts": '♥️',
    "diamonds": '♦️',
    "clubs": '♣️',
    "spades": '♠️',
    "money": '💰',
    "cards": '🃏',
    "trophy": '🏆',
    "fire": '🔥',
    "skull": '💀',
    "clock": '⏳',
    "check": '✅',
    "cross": '❌',
    "home": '🏠',
    "profile": '👤',
    "balance": '💰',
    "top": '🏆',
    "info": 'ℹ️',
    "add": '➕',
    "deposit": '📥',
    "withdraw": '📤',
    "help": '❓',
    "game": '🎮',
    "dice": '🎲',
    "dark": '🌑',
    "light": '☀️',
    "admin": '👑',
    "stats": '📊',
    "settings": '⚙️',
    "warning": '⚠️',
    "error": '❌',
    "success": '✅'
}

# Game state enumerations
class GameState(Enum):
    WAITING = auto()
    JOINING = auto()
    BIDDING = auto()
    AWAITING_CONFIRMATION = auto()
    FINAL_CHOICE = auto()
    FINAL_SWARA_WAIT = auto()
    SHOWDOWN = auto()
    FINISHED = auto()

class GameMode(Enum):
    NORMAL = auto()
    DARK = auto()
    FAST = auto()
    TOURNAMENT = auto()

class PlayerAction(Enum):
    FOLD = auto()
    RAISE = auto()
    CALL = auto()
    CHECK = auto()
    SHOW = auto()
    LOOK = auto()
    SWARA = auto()

# Card class
class Card:
    def __init__(self, rank: str, suit: str):
        self.rank = rank
        self.suit = suit
        self.value = self._calculate_value()
    
    def _calculate_value(self) -> int:
        if self.rank == "Joker":
            return 11
        if self.rank in ['6', '7', '8', '9']:
            return int(self.rank)
        if self.rank in ['10', 'J', 'Q', 'K']:
            return 10
        if self.rank == 'A':
            return 11
        return 0
    
    def __str__(self) -> str:
        if self.rank == "Joker":
            return "Joker 🃏"
        suit_emoji = EMOJI.get(self.suit, self.suit)
        return f"{self.rank}{suit_emoji}"
    
    def __eq__(self, other) -> bool:
        if not isinstance(other, Card):
            return False
        return self.rank == other.rank and self.suit == other.suit

# Player data class
class Player:
    def __init__(self, name: str, balance: int):
        self.name = name
        self.cards: List[Card] = []
        self.balance = balance
        self.mode = GameMode.DARK  # Default to DARK mode
        self.bid = 0
        self.folded = False
        self.shown = False
        self.message_id = None

# Game class for Seka
class SekaGame:
    def __init__(self, chat_id: int, creator_id: int, bet_amount: int, game_mode: GameMode = GameMode.NORMAL):
        self.chat_id = chat_id
        self.creator_id = creator_id
        self.bet_amount = bet_amount
        self.players: Dict[int, Player] = {}
        self.state = GameState.WAITING
        self.current_player = None
        self.deck: List[Card] = []
        self.pot = 0
        self.max_bid = 0
        self.last_raiser = None
        self.game_mode = game_mode
        self.initialize_deck()
        self.joker = Card("Joker", "joker")
        self.show_confirmations: Set[int] = set()
        self.initial_player_count = 0
        self.final_split_set: Set[int] = set()
        self.final_swara_set: Set[int] = set()
        self.final_continue_set: Set[int] = set()
        self.timeouts = {}
        self.created_at = datetime.now()
        self.last_activity = datetime.now()
    
    def initialize_deck(self) -> None:
        suits = ['hearts', 'diamonds', 'clubs', 'spades']
        ranks = ['6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']
        
        self.deck = []
        for suit in suits:
            for rank in ranks:
                # Exclude 7♣ as it's replaced by Joker
                if not (suit == 'clubs' and rank == '7'):
                    self.deck.append(Card(rank, suit))
        
        # Add Joker
        self.deck.append(Card("Joker", "joker"))
    
    def shuffle_deck(self) -> None:
        random.shuffle(self.deck)
    
    async def add_player(self, user_id: int, username: str, context: ContextTypes.DEFAULT_TYPE) -> bool:
        if user_id in self.players or len(self.players) >= 6:
            return False
        
        try:
            balance = await self.get_user_balance(user_id, context)
            if balance < self.bet_amount:
                return False
            
            mode = GameMode.DARK if self.game_mode == GameMode.DARK else GameMode.NORMAL
            self.players[user_id] = Player(username, balance)
            self.players[user_id].mode = mode
            
            self.last_activity = datetime.now()
            return True
        except Exception as e:
            logger.error(f"Error adding player {user_id}: {e}")
            return False
    
    async def start_game(self, context: ContextTypes.DEFAULT_TYPE) -> bool:
        if len(self.players) < 2:
            return False
        
        try:
            # Check balances
            for user_id in list(self.players.keys()):
                balance = await self.get_user_balance(user_id, context)
                if balance < self.bet_amount:
                    return False
                self.players[user_id].balance = balance
            
            # Deduct initial bets
            for user_id in list(self.players.keys()):
                await self.update_user_balance(user_id, -self.bet_amount, "game_bet", context)
                self.players[user_id].balance -= self.bet_amount
            
            self.shuffle_deck()
            self.deal_cards()
            self.state = GameState.BIDDING
            self.current_player = list(self.players.keys())[0]
            self.pot = len(self.players) * self.bet_amount
            self.max_bid = self.bet_amount
            
            for player in self.players.values():
                player.bid = self.bet_amount
            
            self.initial_player_count = len(self.players)
            self.last_activity = datetime.now()
            return True
        except Exception as e:
            logger.error(f"Error starting game in chat {self.chat_id}: {e}")
            return False
    
    def deal_cards(self) -> None:
        cards_per_player = 3
        for player in self.players.values():
            if not player.folded:
                player.cards = []
                for _ in range(cards_per_player):
                    if self.deck:
                        player.cards.append(self.deck.pop())
    
    def calculate_hand_value(self, cards: List[Card]) -> int:
        if not cards:
            return 0
        
        joker_count = len([card for card in cards if card.rank == "Joker"])
        non_joker = [card for card in cards if card.rank != "Joker"]
        
        # Three 6's (with or without joker)
        if ((joker_count >= 1 and len(non_joker) == 2 and all(card.rank == '6' for card in non_joker)) or
                (len(non_joker) == len(cards) and all(card.rank == '6' for card in non_joker))):
            return 34
        
        # Two aces
        if len([card for card in cards if card.rank == 'A']) >= 2:
            return 22
        
        # Three of a kind with joker
        if joker_count >= 1 and len(non_joker) == 2 and non_joker[0].rank == non_joker[1].rank:
            return non_joker[0].value * 3
        
        # Three of a kind without joker
        if non_joker and len(set(card.rank for card in non_joker)) == 1:
            return sum(card.value for card in non_joker) + joker_count * 11
        
        # Highest suit total
        suit_totals = {}
        for suit in ['hearts', 'diamonds', 'clubs', 'spades']:
            suit_totals[suit] = sum(card.value for card in cards if card.suit == suit)
        
        max_suit_total = max(suit_totals.values(), default=0)
        return max_suit_total + joker_count * 11
    
    async def initiate_dispute(self, tied_players: List[int], context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            split_amount = self.pot // len(tied_players)
            for pid in tied_players:
                await self.update_user_balance(pid, split_amount, "game_win_dispute", context)
                self.players[pid].balance += split_amount
            self.state = GameState.FINISHED
            self.last_activity = datetime.now()
        except Exception as e:
            logger.error(f"Error initiating dispute in chat {self.chat_id}: {e}")
    
    async def determine_winner(self, context: ContextTypes.DEFAULT_TYPE) -> Dict[int, int]:
        """Returns a dict mapping winner IDs to their winnings"""
        if self.state != GameState.SHOWDOWN:
            return {}
        
        try:
            active_players = {pid: player for pid, player in self.players.items() if not player.folded}
            
            if not active_players:
                return {}
            
            player_scores = {pid: self.calculate_hand_value(player.cards) for pid, player in active_players.items()}
            
            max_score = max(player_scores.values(), default=0)
            winners = [pid for pid, score in player_scores.items() if score == max_score]
            
            if len(winners) > 1:
                await self.initiate_dispute(winners, context)
                result = {}
                split_amount = self.pot // len(winners)
                for pid in winners:
                    result[pid] = split_amount
                return result
            
            win_amount = self.pot
            winner = winners[0]
            await self.update_user_balance(winner, win_amount, "game_win", context)
            self.players[winner].balance += win_amount
            self.state = GameState.FINISHED
            self.last_activity = datetime.now()
            
            return {winner: win_amount}
        except Exception as e:
            logger.error(f"Error determining winner in chat {self.chat_id}: {e}")
            return {}
    
    async def player_action(self, player_id: int, action: PlayerAction, amount: int, context: ContextTypes.DEFAULT_TYPE) -> Tuple[bool, str]:
        if player_id not in self.players or player_id != self.current_player:
            return False, "Сейчас не ваш ход!"
        
        try:
            player = self.players[player_id]
            
            if action == PlayerAction.FOLD:
                removed_name = player.name
                del self.players[player_id]
                self.initial_player_count = len(self.players)
                message = f"{removed_name} сбрасывает карты и выходит из игры {EMOJI['skull']}"
                await self.next_player()
                self.last_activity = datetime.now()
                return True, message
            
            elif action == PlayerAction.RAISE:
                if amount <= self.max_bid:
                    return False, "Ставка должна быть выше текущей!"
                if amount > player.balance + player.bid:
                    return False, "У вас недостаточно средств!"
                
                amount_to_deduct = amount - player.bid
                await self.update_user_balance(player_id, -amount_to_deduct, "game_raise", context)
                player.balance -= amount_to_deduct
                player.bid = amount
                self.pot += amount_to_deduct
                self.max_bid = amount
                self.last_raiser = player_id
                
                message = f"{player.name} повышает ставку до {amount}{EMOJI['money']} {EMOJI['fire']}"
                await self.next_player()
                self.last_activity = datetime.now()
                return True, message
            
            elif action == PlayerAction.CALL:
                if self.max_bid == 0:
                    return False, "Нельзя поддержать нулевую ставку!"
                if player.bid == self.max_bid:
                    return False, "Вы уже сделали эту ставку!"
                
                diff = self.max_bid - player.bid
                if diff > player.balance:
                    removed_name = player.name
                    del self.players[player_id]
                    self.initial_player_count = len(self.players)
                    message = f"{removed_name} не может поддержать ставку и выбывает {EMOJI['skull']}"
                    await self.next_player()
                    self.last_activity = datetime.now()
                    return True, message
                else:
                    await self.update_user_balance(player_id, -diff, "game_call", context)
                    player.balance -= diff
                    self.pot += diff
                    player.bid = self.max_bid
                    message = f"{player.name} поддерживает ставку {EMOJI['check']}"
                    await self.next_player()
                    self.last_activity = datetime.now()
                    return True, message
            
            elif action == PlayerAction.CHECK:
                if self.max_bid != 0 and player.bid < self.max_bid:
                    return False, "Нельзя пропустить ход при активной ставке!"
                message = f"{player.name} пропускает ход {EMOJI['clock']}"
                await self.next_player()
                self.last_activity = datetime.now()
                return True, message
            
            elif action == PlayerAction.SHOW:
                if not self.last_raiser:
                    return False, "Нельзя вскрыться на первом круге торгов!"
                if player.bid != self.max_bid:
                    return False, "Вы должны поддержать ставку перед вскрытием!"
                
                player.shown = True
                self.last_raiser = None
                self.state = GameState.AWAITING_CONFIRMATION
                self.show_confirmations = {player_id}
                
                message = f"{player.name} требует вскрытия! Ожидаем подтверждения остальных участников."
                self.last_activity = datetime.now()
                return True, message
            
            elif action == PlayerAction.LOOK:
                if self.game_mode == GameMode.DARK:
                    return False, "В темной игре карты скрыты до момента вскрытия!"
                if player.mode != GameMode.DARK:
                    return False, "Вы уже видите свои карты!"
                
                player.mode = GameMode.NORMAL
                self.last_activity = datetime.now()
                return True, f"{player.name} посмотрел свои карты {EMOJI['cards']}"
            
            elif action == PlayerAction.SWARA:
                if self.state != GameState.FINAL_CHOICE:
                    return False, "Свара доступна только на финальном этапе, когда остаются 2 игрока!"
                
                active_players = [pid for pid, p in self.players.items() if not p.folded]
                idx = active_players.index(player_id)
                right_idx = (idx + 1) % len(active_players)
                opponent_id = active_players[right_idx]
                opponent = self.players[opponent_id]
                
                challenger_score = self.calculate_hand_value(player.cards)
                opponent_score = self.calculate_hand_value(opponent.cards)
                
                if challenger_score > opponent_score:
                    opponent.folded = True
                    message = f"{player.name} выиграл свару против {opponent.name} (его {challenger_score} > {opponent_score}) и остается в игре."
                    await self.next_player()
                    self.last_activity = datetime.now()
                    return True, message
                else:
                    player.folded = True
                    message = f"{player.name} проиграл свару против {opponent.name} (его {challenger_score} <= {opponent_score}) и выбывает. Запускается дополнительная партия – свара."
                    await self.initiate_swara(opponent_id, context)
                    self.last_activity = datetime.now()
                    return True, message
            
            return False, "Неизвестное действие"
        
        except Exception as e:
            logger.error(f"Error processing player action in chat {self.chat_id}: {e}")
            return False, "Произошла ошибка при обработке действия. Попробуйте еще раз."
    
    async def initiate_swara(self, swara_initiator_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            swara_participants = {}
            for pid, player in self.players.items():
                if not player.folded:
                    swara_participants[pid] = player
                    player.mode = GameMode.NORMAL
            
            self.state = GameState.SHOWDOWN
            winners = await self.determine_winner(context)
            
            if winners:
                self.state = GameState.FINISHED
            
            self.last_activity = datetime.now()
        except Exception as e:
            logger.error(f"Error initiating swara in chat {self.chat_id}: {e}")
    
    async def next_player(self) -> None:
        try:
            active_players = list(self.players.keys())
            
            if len(active_players) == 2:
                self.state = GameState.FINAL_CHOICE
                self.final_split_set.clear()
                self.final_swara_set.clear()
                self.final_continue_set.clear()
                return
            
            if len(active_players) <= 1:
                self.state = GameState.SHOWDOWN
                return
            
            current_idx = active_players.index(self.current_player)
            next_idx = (current_idx + 1) % len(active_players)
            self.current_player = active_players[next_idx]
        except Exception as e:
            logger.error(f"Error moving to next player in chat {self.chat_id}: {e}")
    
    async def get_user_balance(self, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> int:
        try:
            conn = await get_db_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT balance FROM user_balances WHERE user_id = ?', (user_id,))
            row = cursor.fetchone()
            conn.close()
            return row[0] if row else 0
        except Exception as e:
            logger.error(f"Error getting balance for user {user_id}: {e}")
            return 0
    
    async def update_user_balance(self, user_id: int, amount: int, transaction_type: str, context: ContextTypes.DEFAULT_TYPE) -> bool:
        try:
            conn = await get_db_connection()
            cursor = conn.cursor()
            
            cursor.execute('SELECT balance FROM user_balances WHERE user_id = ?', (user_id,))
            row = cursor.fetchone()
            
            if not row:
                new_balance = amount
                cursor.execute('INSERT INTO user_balances (user_id, balance) VALUES (?, ?)', (user_id, new_balance))
            else:
                current_balance = row[0]
                new_balance = current_balance + amount
                cursor.execute('UPDATE user_balances SET balance = ? WHERE user_id = ?', (new_balance, user_id))
            
            cursor.execute(
                'INSERT INTO transactions (user_id, amount, transaction_type, game_id) VALUES (?, ?, ?, ?)',
                (user_id, amount, transaction_type, self.chat_id)
            )
            
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Error updating balance for user {user_id}: {e}")
            return False
    
    def get_player_cards(self, user_id: int) -> str:
        if user_id in self.players:
            return ' '.join(str(card) for card in self.players[user_id].cards)
        return ""
    
    def clear_timeouts(self) -> None:
        for job_name in self.timeouts:
            try:
                application.job_queue.get_jobs_by_name(job_name)[0].schedule_removal()
            except (IndexError, AttributeError):
                pass
        self.timeouts = {}
    
    def is_inactive(self, minutes: int = 30) -> bool:
        now = datetime.now()
        diff_minutes = (now - self.last_activity).total_seconds() / 60
        return diff_minutes >= minutes

# Active games storage
active_games: Dict[int, SekaGame] = {}

# Database functions
async def get_db_connection():
    """Create a connection to the SQLite database"""
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception as e:
        logger.error(f"Error connecting to database: {e}")
        raise

async def initialize_database():
    """Create necessary tables if they don't exist"""
    try:
        conn = await get_db_connection()
        cursor = conn.cursor()
        
        # Enable foreign keys
        cursor.execute('PRAGMA foreign_keys = ON')
        
        # Create tables
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_balances (
                user_id INTEGER PRIMARY KEY,
                balance INTEGER DEFAULT 0,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount INTEGER,
                transaction_type TEXT,
                game_id INTEGER,
                details TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES user_balances(user_id)
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS game_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                game_id INTEGER,
                bet_amount INTEGER,
                result TEXT,
                profit INTEGER,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES user_balances(user_id)
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Create indexes
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_transactions_user_id ON transactions(user_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_game_history_user_id ON game_history(user_id)')
        
        # Insert default settings if they don't exist
        default_settings = [
            ('min_withdrawal', '100'),
            ('max_withdrawal', '10000'),
            ('min_deposit', '100'),
            ('max_deposit', '10000'),
            ('initial_balance', '1000'),
            ('maintenance_mode', 'false')
        ]
        
        for key, value in default_settings:
            cursor.execute(
                'INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)',
                (key, value)
            )
        
        conn.commit()
        conn.close()
        logger.info('Database initialized successfully')
    except Exception as e:
        logger.error(f"Error initializing database: {e}")
        raise

async def get_setting(key: str) -> Optional[str]:
    """Get a setting value from the database"""
    try:
        conn = await get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT value FROM settings WHERE key = ?', (key,))
        row = cursor.fetchone()
        conn.close()
        return row['value'] if row else None
    except Exception as e:
        logger.error(f"Error getting setting {key}: {e}")
        return None

async def set_setting(key: str, value: str) -> bool:
    """Set a setting value in the database"""
    try:
        conn = await get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            'INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)',
            (key, value)
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Error setting {key} to {value}: {e}")
        return False

# Exchange rate function
async def get_exchange_rate(from_currency: str, to_currency: str) -> float:
    """Get exchange rate between two currencies"""
    if from_currency == "RUB" and to_currency == "USDT":
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get("https://api.exchangerate-api.com/v4/latest/RUB")
                if response.status_code == 200:
                    rate_usd = response.json()['rates']['USD']
                    if rate_usd:
                        return rate_usd
        except Exception as e:
            logger.error(f"Error getting exchange rate: {e}")
        return 1 / 75.0  # Default fallback rate
    return 1.0

# Crypto Bot functions
async def create_cryptobot_invoice(user_id: int, amount: float) -> Optional[str]:
    """Create a payment invoice via Crypto Bot"""
    headers = {
        "Crypto-Pay-API-Token": CRYPTO_BOT_API_KEY,
        "Content-Type": "application/json"
    }
    
    payload = {
        "user_id": user_id,
        "amount": amount,
        "asset": "USDT",
        "description": f"Deposit for user {user_id}",
        "hidden_message": "Thank you for your deposit!",
        "expires_in": 3600
    }
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{CRYPTO_BOT_API_URL}createInvoice",
                json=payload,
                headers=headers
            )
            
            if response.status_code == 200 and response.json()['ok']:
                return response.json()['result']['pay_url']
    except Exception as e:
        logger.error(f"Error creating Crypto Bot invoice: {e}")
    
    return None

async def process_cryptobot_withdrawal(user_id: int, amount: float, wallet: str) -> bool:
    """Process a withdrawal via Crypto Bot"""
    headers = {
        "Crypto-Pay-API-Token": CRYPTO_BOT_API_KEY,
        "Content-Type": "application/json"
    }
    
    payload = {
        "user_id": user_id,
        "asset": "USDT",
        "amount": amount,
        "address": wallet,
        "comment": f"Withdrawal for user {user_id}"
    }
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{CRYPTO_BOT_API_URL}transfer",
                json=payload,
                headers=headers
            )
            
            return response.status_code == 200 and response.json()['ok']
    except Exception as e:
        logger.error(f"Error processing Crypto Bot withdrawal: {e}")
    
    return False

# User data storage in context
async def get_user_data(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> Dict:
    """Get user data from context or initialize it"""
    if not hasattr(context.bot_data, 'user_data'):
        context.bot_data.user_data = {}
    
    if user_id not in context.bot_data.user_data:
        context.bot_data.user_data[user_id] = {'awaiting_withdrawal': False}
    
    return context.bot_data.user_data[user_id]

# Helper function to send player interface
async def send_player_interface(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, game: SekaGame):
    try:
        player = game.players.get(user_id)
        if not player:
            return
        
        keyboard = []
        text_extra = ''
        
        if game.state == GameState.AWAITING_CONFIRMATION:
            if user_id not in game.show_confirmations:
                keyboard.append([
                    InlineKeyboardButton("Подтвердить вскрытие", callback_data="confirm_show"),
                    InlineKeyboardButton("Отказаться от вскрытия", callback_data="decline_show")
                ])
            else:
                keyboard.append([
                    InlineKeyboardButton("Вы подтвердили вскрытие", callback_data="noop"),
                    InlineKeyboardButton("Отказаться от вскрытия", callback_data="decline_show")
                ])
        elif game.state == GameState.FINAL_CHOICE:
            if not player.folded:
                text_extra = "🤔 Финальный этап!\n\n" + \
                            "Остались только 2 активных игрока. Выберите, хотите ли вы " + \
                            "разделить банк, запустить свару или продолжить торги."
                keyboard.append([
                    InlineKeyboardButton("Разделить банк", callback_data="final_split"),
                    InlineKeyboardButton("Запустить свару", callback_data="final_swara"),
                    InlineKeyboardButton("Продолжить торги", callback_data="final_continue")
                ])
            else:
                text_extra = "Вы уже вышли из игры. Нажмите кнопку, чтобы вернуться в игру за 50% от банка."
                keyboard.append([
                    InlineKeyboardButton("Вернуться в игру (50% от банка)", callback_data="rejoin_swara")
                ])
        elif game.state == GameState.BIDDING:
            if game.game_mode != GameMode.DARK:
                if player.mode == GameMode.DARK and not player.shown:
                    keyboard.append([InlineKeyboardButton("👀 Посмотреть карты", callback_data="look")])
                else:
                    min_raise = game.max_bid + (5 if game.game_mode == GameMode.FAST else 10)
                    max_raise = min(player.balance + player.bid, game.bet_amount * 3)
                    raise_buttons = []
                    
                    for amount in [min_raise, min_raise + (game.bet_amount // 2), max_raise]:
                        if amount <= max_raise and amount > game.max_bid:
                            raise_buttons.append(InlineKeyboardButton(f"🔼 {amount}", callback_data=f"raise_{amount}"))
                    
                    if raise_buttons:
                        keyboard.append(raise_buttons)
                    
                    action_buttons = []
                    if game.max_bid > player.bid:
                        action_buttons.append(InlineKeyboardButton("✅ Поддержать", callback_data="call"))
                    else:
                        action_buttons.append(InlineKeyboardButton("⏭ Пропустить", callback_data="check"))
                    
                    action_buttons.append(InlineKeyboardButton("❌ Сбросить", callback_data="fold"))
                    
                    if game.last_raiser and game.max_bid == player.bid:
                        action_buttons.append(InlineKeyboardButton("🃏 Вскрыться", callback_data="show"))
                    
                    keyboard.append(action_buttons)
            else:
                min_raise = game.max_bid + (5 if game.game_mode == GameMode.FAST else 10)
                max_raise = min(player.balance + player.bid, game.bet_amount * 3)
                raise_buttons = []
                
                for amount in [min_raise, min_raise + (game.bet_amount // 2), max_raise]:
                    if amount <= max_raise and amount > game.max_bid:
                        raise_buttons.append(InlineKeyboardButton(f"🔼 {amount}", callback_data=f"raise_{amount}"))
                
                if raise_buttons:
                    keyboard.append(raise_buttons)
                
                action_buttons = []
                if game.max_bid > player.bid:
                    action_buttons.append(InlineKeyboardButton("✅ Поддержать", callback_data="call"))
                else:
                    action_buttons.append(InlineKeyboardButton("⏭ Пропустить", callback_data="check"))
                
                action_buttons.append(InlineKeyboardButton("❌ Сбросить", callback_data="fold"))
                
                if game.last_raiser and game.max_bid == player.bid:
                    action_buttons.append(InlineKeyboardButton("🃏 Вскрыться", callback_data="show"))
                
                keyboard.append(action_buttons)
        
        cards_text = "Скрыты" if game.game_mode == GameMode.DARK and game.state != GameState.SHOWDOWN else game.get_player_cards(user_id)
        
        mode_text = {
            GameMode.NORMAL: "Обычный режим",
            GameMode.DARK: "Режим 'вслепую'",
            GameMode.FAST: "Быстрый режим",
            GameMode.TOURNAMENT: "Турнирный режим"
        }.get(game.game_mode, "Обычный режим")
        
        base_text = f"🎮 Режим: {mode_text}\n" + \
                   f"💰 Ставка: {game.bet_amount} {EMOJI['money']}\n\n" + \
                   f"🎴 Ваши карты: {cards_text}\n\n" + \
                   f"💵 Ваш баланс: {player.balance} {EMOJI['money']}\n" + \
                   f"🏷 Текущая ставка: {player.bid} {EMOJI['money']}\n" + \
                   f"🏦 Банк: {game.pot} {EMOJI['money']}\n\n"
        
        if game.state == GameState.FINISHED:
            base_text += "🎉 Игра завершена! Ожидайте новую игру."
        elif game.state == GameState.AWAITING_CONFIRMATION:
            base_text += "⏳ Ожидается подтверждение вскрытия от всех участников!"
        elif game.state == GameState.FINAL_CHOICE:
            base_text += text_extra
        elif game.state == GameState.FINAL_SWARA_WAIT:
            base_text += "Ожидается присоединение игроков к сваре"
        elif game.current_player == user_id:
            base_text += "🔄 Сейчас ваш ход!"
        else:
            base_text += f"⏳ Ожидаем ход {game.players[game.current_player].name}"
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        try:
            if player.message_id:
                await context.bot.edit_message_text(
                    chat_id=user_id,
                    message_id=player.message_id,
                    text=base_text,
                    reply_markup=reply_markup
                )
            else:
                message = await context.bot.send_message(
                    chat_id=user_id,
                    text=base_text,
                    reply_markup=reply_markup
                )
                player.message_id = message.message_id
        except Exception as e:
            logger.error(f"Error sending interface to player {user_id}: {e}")
    
    except Exception as e:
        logger.error(f"Error in send_player_interface for player {user_id}: {e}")

# Helper function to start game
async def start_game_callback(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    chat_id = job.data['chat_id']
    bet_amount = job.data['bet_amount']
    
    if chat_id not in active_games:
        return
    
    try:
        game = active_games[chat_id]
        if len(game.players) < 2:
            await context.bot.send_message(
                chat_id=chat_id,
                text="❌ Недостаточно игроков для начала игры (нужно минимум 2). Игра отменена."
            )
            del active_games[chat_id]
            return
        
        if await game.start_game(context):
            mode_text = {
                GameMode.NORMAL: "обычном режиме",
                GameMode.DARK: "режиме 'вслепую'",
                GameMode.FAST: "быстром режиме",
                GameMode.TOURNAMENT: "турнирном режиме"
            }.get(game.game_mode, "обычном режиме")
            
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"🔥 Игра началась! ({mode_text})\n" + 
                     f"💰 Ставка: {bet_amount} {EMOJI['money']}\n" + 
                     f"🏦 Банк: {game.pot} {EMOJI['money']}\n" + 
                     f"🎴 Каждый игрок получил по 3 карты\n" + 
                     f"🔄 Первый ход: {game.players[game.current_player].name}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🎮 К игре", url=f"https://t.me/{BOT_USERNAME}")]
                ])
            )
            
            for player_id in game.players:
                await send_player_interface(None, context, player_id, game)
            
            # Set timeout for player turn
            turn_job = context.job_queue.run_once(
                turn_timeout_callback,
                60,  # 1 minute timeout
                data={'chat_id': chat_id},
                name=f"turn_timeout_{chat_id}"
            )
            game.timeouts['turn_timeout'] = f"turn_timeout_{chat_id}"
            
            # Set timeout for game inactivity
            inactivity_job = context.job_queue.run_once(
                inactivity_timeout_callback,
                1800,  # 30 minutes
                data={'chat_id': chat_id},
                name=f"inactivity_timeout_{chat_id}"
            )
            game.timeouts['inactivity_timeout'] = f"inactivity_timeout_{chat_id}"
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text="❌ Не удалось начать игру. Попробуйте позже."
            )
            del active_games[chat_id]
    except Exception as e:
        logger.error(f"Error starting game in chat {chat_id}: {e}")
        await context.bot.send_message(
            chat_id=chat_id,
            text="❌ Произошла ошибка при запуске игры. Попробуйте позже."
        )
        if chat_id in active_games:
            del active_games[chat_id]

# Helper function for player turn timeout
async def turn_timeout_callback(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    chat_id = job.data['chat_id']
    
    if chat_id not in active_games:
        return
    
    try:
        game = active_games[chat_id]
        if game.state == GameState.BIDDING:
            current_player_id = game.current_player
            if current_player_id and current_player_id in game.players:
                player_name = game.players[current_player_id].name
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"⏳ {player_name} слишком долго думает и автоматически пропускает ход."
                )
                
                success, message = await game.player_action(current_player_id, PlayerAction.CHECK, 0, context)
                if success:
                    await context.bot.send_message(chat_id=chat_id, text=message)
                    
                    for pid in game.players:
                        await send_player_interface(None, context, pid, game)
                    
                    if game.state == GameState.SHOWDOWN:
                        await handle_showdown(context, game)
    except Exception as e:
        logger.error(f"Error in turn timeout for chat {chat_id}: {e}")

# Helper function for game inactivity timeout
async def inactivity_timeout_callback(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    chat_id = job.data['chat_id']
    
    if chat_id in active_games:
        try:
            game = active_games[chat_id]
            if game.is_inactive(30):  # 30 minutes
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="⏳ Игра была автоматически завершена из-за отсутствия активности в течение 30 минут."
                )
                
                game.clear_timeouts()
                del active_games[chat_id]
        except Exception as e:
            logger.error(f"Error in inactivity timeout for chat {chat_id}: {e}")

# Helper function for final choice timeout
async def final_choice_timeout_callback(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    chat_id = job.data['chat_id']
    
    if chat_id not in active_games:
        return
    
    try:
        game = active_games[chat_id]
        if game.state == GameState.FINAL_CHOICE:
            active_player_ids = [pid for pid, player in game.players.items() if not player.folded]
            
            game.initialize_deck()
            game.shuffle_deck()
            
            for pid in active_player_ids:
                game.players[pid].cards = []
                for _ in range(3):
                    if game.deck:
                        game.players[pid].cards.append(game.deck.pop())
            
            game.state = GameState.BIDDING
            game.current_player = active_player_ids[0]
            
            await context.bot.send_message(
                chat_id=chat_id,
                text="Время для финального выбора истекло. Новая раздача для двух оставшихся игроков начнётся с обновлёнными картами."
            )
            
            for pid in game.players:
                await send_player_interface(None, context, pid, game)
    except Exception as e:
        logger.error(f"Error in final choice timeout for chat {chat_id}: {e}")

# Helper function for final swara timeout
async def final_swara_timeout_callback(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    chat_id = job.data['chat_id']
    
    if chat_id not in active_games:
        return
    
    try:
        game = active_games[chat_id]
        if game.state == GameState.FINAL_SWARA_WAIT:
            active_player_ids = [pid for pid, player in game.players.items() if not player.folded]
            
            msg = None
            if len(active_player_ids) > 2:
                msg = "Дополнительные игроки присоединились к сваре. Проводим новый раунд с новыми картами."
            else:
                msg = "Никто не присоединился к сваре. Игра продолжается между двумя игроками с новыми картами."
            
            game.initialize_deck()
            game.shuffle_deck()
            
            for pid in active_player_ids:
                game.players[pid].cards = []
                for _ in range(3):
                    if game.deck:
                        game.players[pid].cards.append(game.deck.pop())
            
            game.state = GameState.BIDDING
            game.current_player = active_player_ids[0]
            
            await context.bot.send_message(chat_id=chat_id, text=msg)
            
            for pid in game.players:
                await send_player_interface(None, context, pid, game)
    except Exception as e:
        logger.error(f"Error in final swara timeout for chat {chat_id}: {e}")

# Helper function to handle showdown
async def handle_showdown(context: ContextTypes.DEFAULT_TYPE, game: SekaGame):
    try:
        winners = await game.determine_winner(context)
        
        if winners:
            winner_texts = []
            for pid, amount in winners.items():
                winner_texts.append(f"🏆 {game.players[pid].name} выигрывает {amount} {EMOJI['money']}!")
            winner_text = '\n'.join(winner_texts)
            
            cards_text = []
            for pid, player in game.players.items():
                hand_value = game.calculate_hand_value(player.cards)
                cards_text.append(f"🃏 {player.name}: {game.get_player_cards(pid)} (сила: {hand_value})")
            
            conn = await get_db_connection()
            cursor = conn.cursor()
            
            for pid, player in game.players.items():
                result = 'win' if pid in winners else 'lose'
                profit = winners.get(pid, -game.bet_amount)
                
                cursor.execute(
                    'INSERT INTO game_history (user_id, game_id, bet_amount, result, profit) VALUES (?, ?, ?, ?, ?)',
                    (pid, game.chat_id, game.bet_amount, result, profit)
                )
            
            conn.commit()
            conn.close()
            
            await context.bot.send_message(
                chat_id=game.chat_id,
                text=f"🎉 Игра завершена! 🎉\n{winner_text}\n\nКарты игроков:\n{chr(10).join(cards_text)}\n\n🔄 Для новой игры напишите /begin"
            )
            
            for pid in game.players:
                await send_player_interface(None, context, pid, game)
            
            game.clear_timeouts()
            del active_games[game.chat_id]
    except Exception as e:
        logger.error(f"Error handling showdown in chat {game.chat_id}: {e}")

# Command handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if update.effective_chat.type == 'private':
        try:
            # Register user with initial balance in private chat
            initial_balance = int(await get_setting('initial_balance') or '1000')
            
            conn = await get_db_connection()
            cursor = conn.cursor()
            
            cursor.execute(
                'INSERT OR IGNORE INTO user_balances (user_id, balance, username, first_name, last_name) VALUES (?, ?, ?, ?, ?)',
                (user_id, initial_balance, update.effective_user.username, 
                 update.effective_user.first_name, update.effective_user.last_name)
            )
            
            conn.commit()
            conn.close()
            
            # Show main menu
            keyboard = [
                [InlineKeyboardButton(f"{EMOJI['profile']} Профиль", callback_data='profile')],
                [InlineKeyboardButton(f"{EMOJI['balance']} Баланс", callback_data='balance')],
                [
                    InlineKeyboardButton(f"{EMOJI['deposit']} Пополнить", callback_data='deposit'),
                    InlineKeyboardButton(f"{EMOJI['withdraw']} Вывести", callback_data='withdraw')
                ],
                [InlineKeyboardButton(f"{EMOJI['add']} Добавить в чат", callback_data='add_to_chat')],
                [InlineKeyboardButton(f"{EMOJI['top']} Топ игроков", callback_data='top_players')],
                [InlineKeyboardButton(f"{EMOJI['info']} Информация", callback_data='info')]
            ]
            
            await update.message.reply_text(
                '🏠 Главное меню:',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
        except Exception as e:
            logger.error(f"Error in private start command: {e}")
            await update.message.reply_text("❌ Произошла ошибка. Попробуйте позже.")
            return
    
    # Group chat logic
    try:
        conn = await get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute('SELECT balance FROM user_balances WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        balance = row['balance'] if row else 0
        
        if balance <= 0:
            await update.message.reply_text("❌ У вас недостаточно средств для игры.")
            conn.close()
            return
        
        # Update user info
        cursor.execute(
            'UPDATE user_balances SET username = ?, first_name = ?, last_name = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?',
            (update.effective_user.username, update.effective_user.first_name, update.effective_user.last_name, user_id)
        )
        
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error checking balance: {e}")
        await update.message.reply_text("❌ Произошла ошибка. Попробуйте позже.")
        return
    
    if chat_id in active_games:
        game = active_games[chat_id]
        if game.state in [GameState.WAITING, GameState.JOINING]:
            await update.message.reply_text(
                f"⏳ Игра ожидает начала. Ставка: {game.bet_amount} {EMOJI['money']}\n" +
                "Для присоединения напишите /join"
            )
        else:
            await update.message.reply_text("⏳ Игра уже началась. Дождитесь окончания текущей игры.")
        return
    
    await update.message.reply_text(
        "🎮 Для начала игры укажите ставку в формате:\n" +
        "<code>/begin сумма</code>\n\n" +
        "Пример:\n<code>/begin 100</code>\n\n" +
        "Убедитесь, что у вас достаточно средств.",
        parse_mode='HTML'
    )

async def begin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if chat_id in active_games:
        await update.message.reply_text("⏳ Игра уже создана. Дождитесь окончания текущей.")
        return
    
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(
            "❌ Укажите сумму ставки. Пример:\n<code>/begin 100</code>", 
            parse_mode='HTML'
        )
        return
    
    bet_amount = int(context.args[0])
    if bet_amount <= 0:
        await update.message.reply_text("❌ Ставка должна быть положительной.")
        return
    
    try:
        conn = await get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute('SELECT balance FROM user_balances WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        balance = row['balance'] if row else 0
        
        if balance < bet_amount:
            await update.message.reply_text(
                f"❌ Недостаточно средств. Ваш баланс: {balance} {EMOJI['money']}\n" +
                f"Требуется: {bet_amount} {EMOJI['money']}"
            )
            conn.close()
            return
        
        # Update user info
        cursor.execute(
            'UPDATE user_balances SET username = ?, first_name = ?, last_name = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?',
            (update.effective_user.username, update.effective_user.first_name, update.effective_user.last_name, user_id)
        )
        
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error checking balance: {e}")
        await update.message.reply_text("❌ Произошла ошибка. Попробуйте позже.")
        return
    
    active_games[chat_id] = SekaGame(chat_id, user_id, bet_amount)
    game = active_games[chat_id]
    
    try:
        if await game.add_player(user_id, update.effective_user.first_name, context):
            await update.message.reply_text(
                f"🎮 Игра создана! Ставка: {bet_amount} {EMOJI['money']}\n\n" +
                f"Для присоединения напишите /join\n" +
                f"Участники должны иметь не менее {bet_amount} {EMOJI['money']}\n\n" +
                f"Создатель: {update.effective_user.first_name}\n" +
                f"Игроков: 1\n\n" +
                f"Игра начнется автоматически, когда будет 2+ участника или через 30 секунд."
            )
            
            # Set timeout to start game
            context.job_queue.run_once(
                start_game_callback, 
                30, 
                data={'chat_id': chat_id, 'bet_amount': bet_amount},
                name=f"start_game_{chat_id}"
            )
            game.timeouts['start_game'] = f"start_game_{chat_id}"
        else:
            await update.message.reply_text("❌ Не удалось создать игру. Попробуйте позже.")
            if chat_id in active_games:
                del active_games[chat_id]
    except Exception as e:
        logger.error(f"Error creating game: {e}")
        await update.message.reply_text("❌ Произошла ошибка при создании игры. Попробуйте позже.")
        if chat_id in active_games:
            del active_games[chat_id]

async def join_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if chat_id not in active_games:
        await update.message.reply_text("❌ Нет активной игры для присоединения. Создайте игру с помощью /begin")
        return
    
    game = active_games[chat_id]
    if game.state not in [GameState.WAITING, GameState.JOINING]:
        await update.message.reply_text("⏳ Игра уже началась, нельзя присоединиться.")
        return
    
    if user_id in game.players:
        await update.message.reply_text("⚠️ Вы уже в игре.")
        return
    
    try:
        conn = await get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute('SELECT balance FROM user_balances WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        balance = row['balance'] if row else 0
        
        if balance < game.bet_amount:
            await update.message.reply_text(
                f"❌ Недостаточно средств для присоединения. Требуется: {game.bet_amount} {EMOJI['money']}\n" +
                f"Ваш баланс: {balance} {EMOJI['money']}"
            )
            conn.close()
            return
        
        # Update user info
        cursor.execute(
            'UPDATE user_balances SET username = ?, first_name = ?, last_name = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?',
            (update.effective_user.username, update.effective_user.first_name, update.effective_user.last_name, user_id)
        )
        
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error checking balance: {e}")
        await update.message.reply_text("❌ Произошла ошибка. Попробуйте позже.")
        return
    
    try:
        if await game.add_player(user_id, update.effective_user.first_name, context):
            await update.message.reply_text(
                f"🎉 {update.effective_user.first_name} присоединился к игре! {EMOJI['money']}\n" +
                f"👥 Игроков: {len(game.players)}\n" +
                f"💰 Ставка: {game.bet_amount} {EMOJI['money']}\n\n" +
                f"Игра начнется автоматически, когда будет 2+ участника или через 2 минуты."
            )
            
            if len(game.players) >= 2 and game.state == GameState.WAITING:
                game.state = GameState.JOINING
                
                # If we have 2+ players, start the game sooner
                for job in context.job_queue.get_jobs_by_name(f"start_game_{chat_id}"):
                    job.schedule_removal()
                
                context.job_queue.run_once(
                    start_game_callback,
                    10,  # Start in 10 seconds when we have enough players
                    data={'chat_id': chat_id, 'bet_amount': game.bet_amount},
                    name=f"start_game_{chat_id}"
                )
                game.timeouts['start_game'] = f"start_game_{chat_id}"
        else:
            await update.message.reply_text("❌ Не удалось присоединиться к игре. Максимум 6 игроков.")
    except Exception as e:
        logger.error(f"Error joining game: {e}")
        await update.message.reply_text("❌ Произошла ошибка при присоединении к игре. Попробуйте позже.")

# Fast game command
async def fast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if chat_id in active_games:
        await update.message.reply_text("⏳ Игра уже создана. Дождитесь окончания текущей.")
        return
    
    bet_amount = 50
    try:
        conn = await get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute('SELECT balance FROM user_balances WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        balance = row['balance'] if row else 0
        
        if balance < bet_amount:
            await update.message.reply_text(
                f"❌ Недостаточно средств. Ваш баланс: {balance} {EMOJI['money']}\n" +
                f"Требуется: {bet_amount} {EMOJI['money']}"
            )
            conn.close()
            return
        
        # Update user info
        cursor.execute(
            'UPDATE user_balances SET username = ?, first_name = ?, last_name = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?',
            (update.effective_user.username, update.effective_user.first_name, update.effective_user.last_name, user_id)
        )
        
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error checking balance: {e}")
        await update.message.reply_text("❌ Произошла ошибка. Попробуйте позже.")
        return
    
    active_games[chat_id] = SekaGame(chat_id, user_id, bet_amount, GameMode.FAST)
    game = active_games[chat_id]
    
    try:
        if await game.add_player(user_id, update.effective_user.first_name, context):
            await update.message.reply_text(
                f"⚡ Быстрая игра создана! Ставка: {bet_amount} {EMOJI['money']}\n\n" +
                f"Для присоединения напишите /join\n" +
                f"Участники должны иметь не менее {bet_amount} {EMOJI['money']}\n\n" +
                f"Создатель: {update.effective_user.first_name}\n" +
                f"Игроков: 1\n\n" +
                f"Игра начнется автоматически, когда будет 2+ участника или через 1 минуту."
            )
            
            # Set timeout to start game (60 seconds for fast game)
            context.job_queue.run_once(
                start_game_callback, 
                60, 
                data={'chat_id': chat_id, 'bet_amount': bet_amount},
                name=f"start_game_{chat_id}"
            )
            game.timeouts['start_game'] = f"start_game_{chat_id}"
        else:
            await update.message.reply_text("❌ Не удалось создать игру. Попробуйте позже.")
            del active_games[chat_id]
    except Exception as e:
        logger.error(f"Error creating fast game: {e}")
        await update.message.reply_text("❌ Произошла ошибка при создании игры. Попробуйте позже.")
        if chat_id in active_games:
            del active_games[chat_id]

# Dark game command
async def dark_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if chat_id in active_games:
        await update.message.reply_text("⏳ Игра уже создана. Дождитесь окончания текущей.")
        return
    
    bet_amount = 100
    try:
        conn = await get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute('SELECT balance FROM user_balances WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        balance = row['balance'] if row else 0
        
        if balance < bet_amount:
            await update.message.reply_text(
                f"❌ Недостаточно средств. Ваш баланс: {balance} {EMOJI['money']}\n" +
                f"Требуется: {bet_amount} {EMOJI['money']}"
            )
            conn.close()
            return
        
        # Update user info
        cursor.execute(
            'UPDATE user_balances SET username = ?, first_name = ?, last_name = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?',
            (update.effective_user.username, update.effective_user.first_name, update.effective_user.last_name, user_id)
        )
        
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error checking balance: {e}")
        await update.message.reply_text("❌ Произошла ошибка. Попробуйте позже.")
        return
    
    active_games[chat_id] = SekaGame(chat_id, user_id, bet_amount, GameMode.DARK)
    game = active_games[chat_id]
    
    try:
        if await game.add_player(user_id, update.effective_user.first_name, context):
            await update.message.reply_text(
                f"🌑 Игра в темную создана! Ставка: {bet_amount} {EMOJI['money']}\n\n" +
                f"Для присоединения напишите /join\n" +
                f"Участники должны иметь не менее {bet_amount} {EMOJI['money']}\n\n" +
                f"Создатель: {update.effective_user.first_name}\n" +
                f"Игроков: 1\n\n" +
                f"Игра начнется автоматически, когда будет 2+ участника или через 2 минуты.\n\n" +
                f"Важно: в темной игре ваши карты будут скрыты до момента вскрытия!"
            )
            
            # Set timeout to start game (120 seconds for dark game)
            context.job_queue.run_once(
                start_game_callback, 
                120, 
                data={'chat_id': chat_id, 'bet_amount': bet_amount},
                name=f"start_game_{chat_id}"
            )
            game.timeouts['start_game'] = f"start_game_{chat_id}"
        else:
            await update.message.reply_text("❌ Не удалось создать игру. Попробуйте позже.")
            del active_games[chat_id]
    except Exception as e:
        logger.error(f"Error creating dark game: {e}")
        await update.message.reply_text("❌ Произошла ошибка при создании игры. Попробуйте позже.")
        if chat_id in active_games:
            del active_games[chat_id]

# Help command
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = (
        f"{EMOJI['help']} <b>Доступные команды:</b>\n\n"
        "/start - Начать игру или зарегистрироваться\n"
        "/begin - Начать игру с указанием ставки\n"
        "/join - Присоединиться к игре\n"
        "/fast - Начать быструю игру (меньшие ставки)\n"
        "/dark - Начать игру в режиме 'вслепую' (карты скрыты до вскрытия)\n"
        "/help - Показать это сообщение\n\n"
        "🎮 <b>Действия в игре:</b>\n"
        "- ✅ Поддержать (Call) - уравнять ставку\n"
        "- 🔼 Повысить (Raise) - увеличить ставку\n"
        "- ⏭ Пропустить (Check) - пропустить ход\n"
        "- ❌ Сбросить (Fold) - выйти из раздачи\n"
        "- 🃏 Вскрыться (Show) - инициировать вскрытие (с подтверждением)\n"
        "- ⚔️ Свара - запустить сравнительную партию с правым игроком (только в финальном выборе)\n\n"
        "В финале, если изначально было больше 2 участников, когда остаётся 2 игрока:\n"
        "• Активным игрокам будет предложено: разделить банк, запустить свару или продолжить торги.\n"
        "• Выбывшим игрокам будет отправлено уведомление: вернуться в игру за 50% от банка, в течение 1 минуты.\n\n"
        "ℹ️ Полные правила: /info"
    )
    
    await update.message.reply_text(help_text, parse_mode='HTML')

# Info command
async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    info_text = (
        "ℹ️ <b>Информация и правила игры:</b>\n\n"
        "🎴 <b>Seka</b> - карточная игра с элементами покера и блефа.\n\n"
        "📌 <b>Основные правила:</b>\n"
        "- Каждый игрок получает 3 карты\n"
        "- Можно играть вслепую (не глядя на карты) за бонусы\n"
        "- Joker – дикая карта, заменяющая 7♣; имеет 11 очков и суммируется с любым высоким значением\n"
        "- Три шестерки – особая комбинация (34 очка)\n"
        "- Два туза дают 22 очка\n\n"
        "💰 <b>Игра на деньги:</b>\n"
        "- Для начала игры нужен положительный баланс\n"
        "- Создатель игры указывает ставку\n"
        "- Участники должны иметь сумму не меньше ставки\n"
        "- Выигрыш зачисляется на баланс\n\n"
        "🔄 <b>Как начать игру:</b>\n"
        "1. Добавьте бота в группу\n"
        "2. Напишите /start для регистрации\n"
        "3. Создатель пишет /begin и указывает ставку\n"
        "4. Или выберите режим: /fast, /dark\n\n"
        "❓ По всем вопросам: @support"
    )
    
    await update.message.reply_text(info_text, parse_mode='HTML')

# Admin command
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    
    if user_id not in ADMIN_IDS:
        return
    
    keyboard = [
        [InlineKeyboardButton(f"{EMOJI['stats']} Статистика", callback_data='admin_stats')],
        [InlineKeyboardButton(f"{EMOJI['settings']} Настройки", callback_data='admin_settings')],
        [InlineKeyboardButton(f"{EMOJI['balance']} Управление балансами", callback_data='admin_balances')],
        [InlineKeyboardButton(f"{EMOJI['game']} Управление играми", callback_data='admin_games')]
    ]
    
    await update.message.reply_text(
        f"{EMOJI['admin']} Панель администратора:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# Callback query handler
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    data = query.data
    
    # Menu callbacks
    if data == 'profile':
        await handle_profile(update, context)
    elif data == 'balance':
        await handle_balance(update, context)
    elif data == 'deposit':
        await handle_deposit(update, context)
    elif data.startswith('deposit_'):
        await handle_deposit_amount(update, context)
    elif data == 'withdraw':
        await handle_withdraw(update, context)
    elif data == 'add_to_chat':
        await handle_add_to_chat(update, context)
    elif data == 'top_players':
        await handle_top_players(update, context)
    elif data == 'info':
        await handle_info(update, context)
    elif data == 'back_to_menu':
        await handle_back_to_menu(update, context)
    
    # Admin callbacks
    elif data == 'admin':
        await handle_admin_menu(update, context)
    elif data == 'admin_stats':
        await handle_admin_stats(update, context)
    elif data == 'admin_settings':
        await handle_admin_settings(update, context)
    elif data == 'admin_balances':
        await handle_admin_balances(update, context)
    elif data == 'admin_games':
        await handle_admin_games(update, context)
    
    # Game action handlers
    elif data in ['fold', 'call', 'check', 'show', 'look', 'noop']:
        await handle_game_action(update, context, data)
    elif data.startswith('raise_'):
        amount = int(data.split('_')[1])
        await handle_game_raise(update, context, amount)
    elif data == 'confirm_show':
        await handle_confirm_show(update, context)
    elif data == 'decline_show':
        await handle_decline_show(update, context)
    elif data in ['final_split', 'final_swara', 'final_continue']:
        await handle_final_choice(update, context, data)
    elif data == 'rejoin_swara':
        await handle_rejoin_swara(update, context)

# Menu callback handlers
async def handle_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    
    try:
        conn = await get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute('SELECT balance FROM user_balances WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        balance = row['balance'] if row else 0
        
        cursor.execute('SELECT COUNT(*) as count FROM game_history WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        games_played = row['count'] if row else 0
        
        cursor.execute('SELECT COUNT(*) as count FROM game_history WHERE user_id = ? AND result = "win"', (user_id,))
        row = cursor.fetchone()
        games_won = row['count'] if row else 0
        
        cursor.execute('SELECT SUM(amount) as total FROM transactions WHERE user_id = ? AND amount > 0', (user_id,))
        row = cursor.fetchone()
        total_profit = row['total'] if row and row['total'] is not None else 0
        
        conn.close()
        
        await query.edit_message_text(
            f"👤 Ваш профиль:\n\n" +
            f"🆔 ID: {user_id}\n" +
            f"💰 Баланс: {balance} {EMOJI['money']}\n" +
            f"🎮 Игр сыграно: {games_played}\n" +
            f"🏆 Побед: {games_won}\n" +
            f"💵 Общий выигрыш: {total_profit} {EMOJI['money']}\n\n",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data='back_to_menu')]])
        )
    except Exception as e:
        logger.error(f"Error showing profile: {e}")
        await query.edit_message_text(
            "❌ Произошла ошибка при загрузке профиля. Попробуйте позже.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data='back_to_menu')]])
        )

async def handle_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    
    try:
        conn = await get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute('SELECT balance FROM user_balances WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        balance = row['balance'] if row else 0
        
        conn.close()
        
        await query.edit_message_text(
            f"💰 Ваш баланс: {balance} {EMOJI['money']}",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(f"{EMOJI['deposit']} Пополнить", callback_data='deposit'),
                    InlineKeyboardButton(f"{EMOJI['withdraw']} Вывести", callback_data='withdraw')
                ],
                [InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data='back_to_menu')]
            ])
        )
    except Exception as e:
        logger.error(f"Error showing balance: {e}")
        await query.edit_message_text(
            "❌ Произошла ошибка при загрузке баланса. Попробуйте позже.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data='back_to_menu')]])
        )

async def handle_deposit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    
    keyboard = [
        [
            InlineKeyboardButton("100 ₽", callback_data="deposit_100"),
            InlineKeyboardButton("500 ₽", callback_data="deposit_500")
        ],
        [
            InlineKeyboardButton("1000 ₽", callback_data="deposit_1000"),
            InlineKeyboardButton("5000 ₽", callback_data="deposit_5000")
        ],
        [InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data='back_to_menu')]
    ]
    
    await query.edit_message_text(
        "📥 Выберите сумму для пополнения:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def handle_deposit_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    amount = int(query.data.split('_')[1])
    
    try:
        rate = await get_exchange_rate("RUB", "USDT")
        usdt_amount = float(round(amount * rate, 4))
        
        invoice_url = await create_cryptobot_invoice(user_id, usdt_amount)
        if invoice_url:
            await query.edit_message_text(
                f"📥 Для пополнения на {amount} ₽ (~ {usdt_amount} USDT):\n\n" +
                f"1. Перейдите по ссылке: {invoice_url}\n" +
                f"2. Оплатите счет в криптовалюте\n" +
                f"3. Средства будут зачислены автоматически\n\n" +
                f"Обычно зачисление занимает 1-5 минут.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Проверить баланс", callback_data="balance")],
                    [InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data='back_to_menu')]
                ])
            )
        else:
            await query.edit_message_text(
                "❌ Не удалось создать счет для оплаты. Попробуйте позже.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data='back_to_menu')]
                ])
            )
    except Exception as e:
        logger.error(f"Error processing deposit: {e}")
        await query.edit_message_text(
            "❌ Произошла ошибка при создании счета. Попробуйте позже.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data='back_to_menu')]
            ])
        )

async def handle_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    
    try:
        conn = await get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute('SELECT balance FROM user_balances WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        balance = row['balance'] if row else 0
        
        conn.close()
        
        min_withdraw = int(await get_setting('min_withdrawal') or '100')
        
        if balance < min_withdraw:
            await query.edit_message_text(
                f"❌ Минимальная сумма вывода: {min_withdraw} ₽\nВаш баланс: {balance} ₽",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data='back_to_menu')]
                ])
            )
            return
        
        user_data = await get_user_data(context, user_id)
        user_data['awaiting_withdrawal'] = True
        
        await query.edit_message_text(
            f"📤 Для вывода средств укажите:\n\n" +
            f"1. Сумму в рублях (не менее {min_withdraw} ₽)\n" +
            f"2. Ваш крипто-кошелек (USDT TRC20)\n\n" +
            f"Пример:\n<code>7500 ₽: TAbCdEfGhIjKlMnOpQrStUvWxYz123456</code>\n\n" +
            f"Ваш текущий баланс: {balance} ₽",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data='back_to_menu')]
            ])
        )
    except Exception as e:
        logger.error(f"Error getting balance for withdraw: {e}")
        await query.edit_message_text(
            "❌ Произошла ошибка при проверке баланса. Попробуйте позже.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data='back_to_menu')]
            ])
        )

async def handle_add_to_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    
    await query.edit_message_text(
        "📌 Как добавить бота в чат:\n\n" +
        "1. Откройте нужный чат\n" +
        "2. Нажмите на название чата вверху\n" +
        "3. Выберите 'Добавить участников'\n" +
        f"4. Найдите @{BOT_USERNAME} и добавьте\n\n" +
        "После добавления напишите /start в чате для активации бота.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data='back_to_menu')]
        ])
    )

async def handle_top_players(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    
    try:
        conn = await get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute(
            'SELECT user_id, balance, username, first_name FROM user_balances ORDER BY balance DESC LIMIT 10'
        )
        top_players = cursor.fetchall()
        
        if not top_players:
            await query.edit_message_text(
                "🏆 Топ игроков пока пуст. Будьте первым!",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data='back_to_menu')]
                ])
            )
            conn.close()
            return
        
        text = "🏆 Топ игроков по балансу:\n\n"
        
        for i, player in enumerate(top_players):
            name = f"@{player['username']}" if player['username'] else (player['first_name'] or f"Игрок {player['user_id']}")
            
            cursor.execute(
                'SELECT COUNT(*) as count FROM game_history WHERE user_id = ?', 
                (player['user_id'],)
            )
            row = cursor.fetchone()
            games_played = row['count'] if row else 0
            
            text += f"{i + 1}. {name} - {player['balance']} {EMOJI['money']} (игр: {games_played})\n"
        
        conn.close()
        
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data='back_to_menu')]
            ])
        )
    except Exception as e:
        logger.error(f"Error showing top players: {e}")
        await query.edit_message_text(
            "❌ Произошла ошибка при загрузке топа игроков. Попробуйте позже.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data='back_to_menu')]
            ])
        )

async def handle_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    
    info_text = (
        "ℹ️ <b>Информация и правила игры:</b>\n\n"
        "🎴 <b>Seka</b> - карточная игра с элементами покера и блефа.\n\n"
        "📌 <b>Основные правила:</b>\n"
        "- Каждый игрок получает 3 карты\n"
        "- Можно играть вслепую (не глядя на карты) за бонусы\n"
        "- Joker – дикая карта, заменяющая 7♣; имеет 11 очков и суммируется с любым высоким значением\n"
        "- Три шестерки – особая комбинация (34 очка)\n"
        "- Два туза дают 22 очка\n\n"
        "💰 <b>Игра на деньги:</b>\n"
        "- Для начала игры нужен положительный баланс\n"
        "- Создатель игры указывает ставку\n"
        "- Участники должны иметь сумму не меньше ставки\n"
        "- Выигрыш зачисляется на баланс\n\n"
        "🔄 <b>Как начать игру:</b>\n"
        "1. Добавьте бота в группу\n"
        "2. Напишите /start для регистрации\n"
        "3. Создатель пишет /begin и указывает ставку\n"
        "4. Или выберите режим: /fast, /dark\n\n"
        "❓ По всем вопросам: @support"
    )
    
    await query.edit_message_text(
        info_text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data='back_to_menu')]
        ])
    )

async def handle_back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    
    keyboard = [
        [InlineKeyboardButton(f"{EMOJI['profile']} Профиль", callback_data='profile')],
        [InlineKeyboardButton(f"{EMOJI['balance']} Баланс", callback_data='balance')],
        [
            InlineKeyboardButton(f"{EMOJI['deposit']} Пополнить", callback_data='deposit'),
            InlineKeyboardButton(f"{EMOJI['withdraw']} Вывести", callback_data='withdraw')
        ],
        [InlineKeyboardButton(f"{EMOJI['add']} Добавить в чат", callback_data='add_to_chat')],
        [InlineKeyboardButton(f"{EMOJI['top']} Топ игроков", callback_data='top_players')],
        [InlineKeyboardButton(f"{EMOJI['info']} Информация", callback_data='info')]
    ]
    
    await query.edit_message_text(
        '🏠 Главное меню:',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# Admin menu handlers
async def handle_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    
    if user_id not in ADMIN_IDS:
        return
    
    keyboard = [
        [InlineKeyboardButton(f"{EMOJI['stats']} Статистика", callback_data='admin_stats')],
        [InlineKeyboardButton(f"{EMOJI['settings']} Настройки", callback_data='admin_settings')],
        [InlineKeyboardButton(f"{EMOJI['balance']} Управление балансами", callback_data='admin_balances')],
        [InlineKeyboardButton(f"{EMOJI['game']} Управление играми", callback_data='admin_games')]
    ]
    
    await query.edit_message_text(
        f"{EMOJI['admin']} Панель администратора:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def handle_admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    
    if user_id not in ADMIN_IDS:
        return
    
    try:
        conn = await get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute('SELECT COUNT(*) as count FROM user_balances')
        row = cursor.fetchone()
        user_count = row['count'] if row else 0
        
        active_games_count = len(active_games)
        
        cursor.execute('SELECT SUM(balance) as total FROM user_balances')
        row = cursor.fetchone()
        total_balance = row['total'] if row and row['total'] is not None else 0
        
        cursor.execute('SELECT SUM(amount) as total FROM transactions WHERE transaction_type = "deposit"')
        row = cursor.fetchone()
        total_deposits = row['total'] if row and row['total'] is not None else 0
        
        cursor.execute('SELECT SUM(amount) as total FROM transactions WHERE transaction_type = "withdrawal"')
        row = cursor.fetchone()
        total_withdrawals = abs(row['total']) if row and row['total'] is not None else 0
        
        cursor.execute('SELECT COUNT(*) as count FROM game_history')
        row = cursor.fetchone()
        games_played = row['count'] if row else 0
        
        conn.close()
        
        # Calculate uptime
        uptime_seconds = time.time() - context.bot_data.get('start_time', time.time())
        uptime_text = format_uptime(uptime_seconds)
        
        await query.edit_message_text(
            f"{EMOJI['stats']} <b>Статистика бота:</b>\n\n" +
            f"👥 Всего пользователей: {user_count}\n" +
            f"🎮 Активных игр: {active_games_count}\n" +
            f"💰 Общий баланс: {total_balance} ₽\n" +
            f"📥 Всего депозитов: {total_deposits} ₽\n" +
            f"📤 Всего выводов: {total_withdrawals} ₽\n" +
            f"🎲 Всего игр сыграно: {games_played}\n\n" +
            f"⏱ Время работы: {uptime_text}",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data='admin')]
            ])
        )
    except Exception as e:
        logger.error(f"Error getting admin stats: {e}")
        
        await query.edit_message_text(
            "❌ Произошла ошибка при загрузке статистики.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data='admin')]
            ])
        )

async def handle_admin_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    
    if user_id not in ADMIN_IDS:
        return
    
    # This is a placeholder. In a real implementation, you would show settings options here
    await query.edit_message_text(
        "⚙️ Настройки бота:\n\n"
        "Здесь будут настройки бота.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data='admin')]
        ])
    )

async def handle_admin_balances(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    
    if user_id not in ADMIN_IDS:
        return
    
    # This is a placeholder. In a real implementation, you would show balance management options here
    await query.edit_message_text(
        "💰 Управление балансами:\n\n"
        "Здесь будут инструменты для управления балансами пользователей.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data='admin')]
        ])
    )

async def handle_admin_games(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    
    if user_id not in ADMIN_IDS:
        return
    
    # This is a placeholder. In a real implementation, you would show game management options here
    await query.edit_message_text(
        "🎮 Управление играми:\n\n"
        f"Активных игр: {len(active_games)}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data='admin')]
        ])
    )

# Game action handlers
async def handle_game_action(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    
    game = None
    for g in active_games.values():
        if user_id in g.players:
            game = g
            break
    
    if not game:
        await query.edit_message_text(
            "❌ Игра не найдена или завершена.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{EMOJI['home']} В меню", callback_data='back_to_menu')]
            ])
        )
        return
    
    if action == 'noop':
        return
    
    action_map = {
        'fold': PlayerAction.FOLD,
        'call': PlayerAction.CALL,
        'check': PlayerAction.CHECK,
        'show': PlayerAction.SHOW,
        'look': PlayerAction.LOOK
    }
    
    success, message = await game.player_action(user_id, action_map[action], 0, context)
    
    if success:
        await context.bot.send_message(chat_id=game.chat_id, text=message)
        
        for pid in game.players:
            await send_player_interface(update, context, pid, game)
        
        if game.state == GameState.SHOWDOWN:
            await handle_showdown(context, game)
        elif game.state == GameState.FINAL_CHOICE:
            # Set timeout for final choice
            for job in context.job_queue.get_jobs_by_name(f"final_choice_timeout_{game.chat_id}"):
                job.schedule_removal()
            
            context.job_queue.run_once(
                final_choice_timeout_callback,
                60,  # 1 minute
                data={'chat_id': game.chat_id},
                name=f"final_choice_timeout_{game.chat_id}"
            )
            game.timeouts['final_choice_timeout'] = f"final_choice_timeout_{game.chat_id}"
    else:
        await query.answer(message, show_alert=True)

async def handle_game_raise(update: Update, context: ContextTypes.DEFAULT_TYPE, amount: int) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    
    game = None
    for g in active_games.values():
        if user_id in g.players:
            game = g
            break
    
    if not game:
        await query.edit_message_text(
            "❌ Игра не найдена или завершена.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{EMOJI['home']} В меню", callback_data='back_to_menu')]
            ])
        )
        return
    
    success, message = await game.player_action(user_id, PlayerAction.RAISE, amount, context)
    
    if success:
        await context.bot.send_message(chat_id=game.chat_id, text=message)
        
        for pid in game.players:
            await send_player_interface(update, context, pid, game)
    else:
        await query.answer(message, show_alert=True)

async def handle_confirm_show(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    
    game = None
    for g in active_games.values():
        if user_id in g.players:
            game = g
            break
    
    if not game:
        await query.edit_message_text(
            "❌ Игра не найдена или завершена.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{EMOJI['home']} В меню", callback_data='back_to_menu')]
            ])
        )
        return
    
    if game.state != GameState.AWAITING_CONFIRMATION:
        await query.answer("Неверное состояние игры для подтверждения вскрытия!", show_alert=True)
        return
    
    if user_id in game.show_confirmations:
        await query.answer("Вы уже подтвердили вскрытие!", show_alert=True)
        return
    
    game.show_confirmations.add(user_id)
    await context.bot.send_message(
        chat_id=game.chat_id,
        text=f"{game.players[user_id].name} подтвердил вскрытие!"
    )
    
    active_players = [pid for pid, player in game.players.items() if not player.folded]
    if len(game.show_confirmations) == len(active_players):
        game.state = GameState.SHOWDOWN
        await handle_showdown(context, game)
    else:
        for pid in game.players:
            await send_player_interface(update, context, pid, game)

async def handle_decline_show(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    
    game = None
    for g in active_games.values():
        if user_id in g.players:
            game = g
            break
    
    if not game:
        await query.edit_message_text(
            "❌ Игра не найдена или завершена.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{EMOJI['home']} В меню", callback_data='back_to_menu')]
            ])
        )
        return
    
    game.state = GameState.BIDDING
    game.show_confirmations.clear()
    
    await context.bot.send_message(
        chat_id=game.chat_id,
        text=f"{game.players[user_id].name} отказался от вскрытия. Игра возвращается к торгам."
    )
    
    for pid in game.players:
        await send_player_interface(update, context, pid, game)

async def handle_final_choice(update: Update, context: ContextTypes.DEFAULT_TYPE, choice: str) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    
    game = None
    for g in active_games.values():
        if user_id in g.players:
            game = g
            break
    
    if not game:
        await query.edit_message_text(
            "❌ Игра не найдена или завершена.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{EMOJI['home']} В меню", callback_data='back_to_menu')]
            ])
        )
        return
    
    if game.state != GameState.FINAL_CHOICE:
        await query.answer("Неверное состояние игры для финального выбора!", show_alert=True)
        return
    
    if choice == 'final_split':
        game.final_split_set.add(user_id)
        
        await context.bot.send_message(
            chat_id=game.chat_id,
            text=f"{game.players[user_id].name} выбрал разделить банк."
        )
        
        active_players = [pid for pid, player in game.players.items() if not player.folded]
        
        if len(game.final_split_set) == len(active_players):
            split_amount = game.pot // len(active_players)
            
            for pid in active_players:
                await game.update_user_balance(pid, split_amount, "final_split", context)
                game.players[pid].balance += split_amount
            
            game.state = GameState.FINISHED
            
            await context.bot.send_message(
                chat_id=game.chat_id,
                text=f"Банк разделен поровну: {split_amount} {EMOJI['money']} каждому. Игра окончена."
            )
            
            for pid in game.players:
                await send_player_interface(update, context, pid, game)
            
            game.clear_timeouts()
            del active_games[game.chat_id]
        else:
            for pid in game.players:
                await send_player_interface(update, context, pid, game)
    
    elif choice == 'final_swara':
        game.final_swara_set.add(user_id)
        
        await context.bot.send_message(
            chat_id=game.chat_id,
            text=f"{game.players[user_id].name} выбрал запуск свары."
        )
        
        game.state = GameState.FINAL_SWARA_WAIT
        
        # Set timeout for swara wait
        for job in context.job_queue.get_jobs_by_name(f"final_swara_timeout_{game.chat_id}"):
            job.schedule_removal()
        
        context.job_queue.run_once(
            final_swara_timeout_callback,
            60,  # 1 minute
            data={'chat_id': game.chat_id},
            name=f"final_swara_timeout_{game.chat_id}"
        )
        game.timeouts['final_swara_timeout'] = f"final_swara_timeout_{game.chat_id}"
        
        for pid in game.players:
            await send_player_interface(update, context, pid, game)
    
    elif choice == 'final_continue':
        if user_id not in game.final_continue_set:
            if len(game.final_continue_set) < 2:
                game.final_continue_set.add(user_id)
                
                await context.bot.send_message(
                    chat_id=game.chat_id,
                    text=f"{game.players[user_id].name} выбрал продолжить торги."
                )
            else:
                game.players[user_id].folded = True
                
                await context.bot.send_message(
                    chat_id=user_id,
                    text="Свара не будет запущена, вы проиграли."
                )
        
        active_players = [pid for pid, player in game.players.items() if not player.folded]
        all_choices = set().union(game.final_split_set, game.final_swara_set, game.final_continue_set)
        
        if len(all_choices) == len(active_players):
            if len(game.final_continue_set) == 2:
                for pid in active_players:
                    if pid not in game.final_continue_set:
                        game.players[pid].folded = True
                        
                        try:
                            await context.bot.send_message(
                                chat_id=pid,
                                text="Свара не будет запущена, вы проиграли."
                            )
                        except Exception as e:
                            logger.error(f"Error sending message to player {pid}: {e}")
                
                continue_players = list(game.final_continue_set)
                
                game.initialize_deck()
                game.shuffle_deck()
                
                for pid in continue_players:
                    game.players[pid].cards = []
                    for _ in range(3):
                        if game.deck:
                            game.players[pid].cards.append(game.deck.pop())
                
                game.state = GameState.BIDDING
                game.current_player = continue_players[0]
                
                await context.bot.send_message(
                    chat_id=game.chat_id,
                    text="Двое игроков решили продолжить торги. Начинается новый раунд с новыми картами!"
                )
                
                for pid in game.players:
                    await send_player_interface(update, context, pid, game)
            else:
                await context.bot.send_message(
                    chat_id=game.chat_id,
                    text="Для продолжения торгов должно быть выбрано ровно 2 игрока!"
                )
        else:
            for pid in game.players:
                await send_player_interface(update, context, pid, game)

async def handle_rejoin_swara(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    
    game = None
    for g in active_games.values():
        if user_id in g.players:
            game = g
            break
    
    if not game:
        await query.edit_message_text(
            "❌ Игра не найдена или завершена.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{EMOJI['home']} В меню", callback_data='back_to_menu')]
            ])
        )
        return
    
    if game.players[user_id].folded:
        cost = game.pot // 2
        
        if await game.update_user_balance(user_id, -cost, "rejoin", context):
            game.players[user_id].folded = False
            
            await context.bot.send_message(
                chat_id=game.chat_id,
                text=f"{game.players[user_id].name} вернулся в игру, внеся {cost} {EMOJI['money']}."
            )
            
            for pid in game.players:
                await send_player_interface(update, context, pid, game)
        else:
            await query.answer("Ошибка списания средств для возврата.", show_alert=True)

# Message handler for withdrawal requests
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != 'private':
        return
    
    user_id = update.effective_user.id
    user_data = await get_user_data(context, user_id)
    
    if user_data.get('awaiting_withdrawal'):
        text = update.message.text.strip()
        
        try:
            parts = text.split(':')
            
            if len(parts) != 2:
                raise ValueError("Неверный формат")
            
            amount_part = parts[0].strip()
            amount_str = ''.join(c for c in amount_part if c.isdigit() or c == '.')
            
            if not amount_str:
                raise ValueError("Неверная сумма")
            
            amount_rub = float(amount_str)
            wallet = parts[1].strip()
            
            min_withdraw = int(await get_setting('min_withdrawal') or '100')
            max_withdraw = int(await get_setting('max_withdrawal') or '10000')
            
            conn = await get_db_connection()
            cursor = conn.cursor()
            
            cursor.execute('SELECT balance FROM user_balances WHERE user_id = ?', (user_id,))
            row = cursor.fetchone()
            balance = row['balance'] if row else 0
            
            conn.close()
            
            if balance < amount_rub:
                await update.message.reply_text(
                    f"❌ Недостаточно средств. Ваш баланс: {balance} ₽",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data='back_to_menu')]
                    ])
                )
                return
            
            if amount_rub < min_withdraw:
                await update.message.reply_text(
                    f"❌ Минимальная сумма вывода: {min_withdraw} ₽",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data='back_to_menu')]
                    ])
                )
                return
            
            if amount_rub > max_withdraw:
                await update.message.reply_text(
                    f"❌ Максимальная сумма вывода: {max_withdraw} ₽",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data='back_to_menu')]
                    ])
                )
                return
            
            rate = await get_exchange_rate("RUB", "USDT")
            usdt_amount = float(round(amount_rub * rate, 4))
            
            success = await process_cryptobot_withdrawal(user_id, usdt_amount, wallet)
            
            if success:
                conn = await get_db_connection()
                cursor = conn.cursor()
                
                cursor.execute(
                    'UPDATE user_balances SET balance = balance - ? WHERE user_id = ?',
                    (amount_rub, user_id)
                )
                
                cursor.execute(
                    'INSERT INTO transactions (user_id, amount, transaction_type, details) VALUES (?, ?, ?, ?)',
                    (user_id, -amount_rub, "withdrawal", f"USDT: {usdt_amount}, Wallet: {wallet}")
                )
                
                conn.commit()
                conn.close()
                
                await update.message.reply_text(
                    f"✅ Запрос на вывод {amount_rub} ₽ (~ {usdt_amount} USDT) обработан!\n\n" +
                    f"Средства будут отправлены на кошелек:\n<code>{wallet}</code>\n\n" +
                    f"Обычно перевод занимает 5-15 минут.",
                    parse_mode='HTML',
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data='back_to_menu')]
                    ])
                )
            else:
                await update.message.reply_text(
                    "❌ Не удалось обработать вывод. Попробуйте позже.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data='back_to_menu')]
                    ])
                )
        except Exception as e:
            logger.error(f"Error processing withdrawal: {e}")
            
            await update.message.reply_text(
                "❌ Неверный формат запроса. Пример:\n\n" +
                "<code>7500 ₽: TAbCdEfGhIjKlMnOpQrStUvWxYz123456</code>",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data='back_to_menu')]
                ])
            )
        
        user_data['awaiting_withdrawal'] = False
    else:
        # Show main menu for other messages in private chat
        keyboard = [
            [InlineKeyboardButton(f"{EMOJI['profile']} Профиль", callback_data='profile')],
            [InlineKeyboardButton(f"{EMOJI['balance']} Баланс", callback_data='balance')],
            [
                InlineKeyboardButton(f"{EMOJI['deposit']} Пополнить", callback_data='deposit'),
                InlineKeyboardButton(f"{EMOJI['withdraw']} Вывести", callback_data='withdraw')
            ],
            [InlineKeyboardButton(f"{EMOJI['add']} Добавить в чат", callback_data='add_to_chat')],
            [InlineKeyboardButton(f"{EMOJI['top']} Топ игроков", callback_data='top_players')],
            [InlineKeyboardButton(f"{EMOJI['info']} Информация", callback_data='info')]
        ]
        
        await update.message.reply_text(
            '🏠 Главное меню:',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

# Helper function to format uptime
def format_uptime(seconds: float) -> str:
    days = int(seconds // 86400)
    seconds %= 86400
    hours = int(seconds // 3600)
    seconds %= 3600
    minutes = int(seconds // 60)
    seconds = int(seconds % 60)
    
    return f"{days}д {hours}ч {minutes}м {seconds}с"

# Periodic cleanup of inactive games
async def cleanup_inactive_games(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        chat_ids_to_remove = []
        
        for chat_id, game in active_games.items():
            if game.is_inactive(30):  # 30 minutes
                logger.info(f"Removing inactive game in chat {chat_id}")
                game.clear_timeouts()
                chat_ids_to_remove.append(chat_id)
        
        for chat_id in chat_ids_to_remove:
            del active_games[chat_id]
    except Exception as e:
        logger.error(f"Error in inactive games cleanup: {e}")

# Main function
async def main() -> None:
    try:
        # Initialize database
        await initialize_database()
        
        # Create the Application
        application = Application.builder().token(BOT_TOKEN).build()
        
        # Store start time
        application.bot_data['start_time'] = time.time()
        
        # Add command handlers
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("begin", begin_command))
        application.add_handler(CommandHandler("join", join_command))
        application.add_handler(CommandHandler("fast", fast_command))
        application.add_handler(CommandHandler("dark", dark_command))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("info", info_command))
        application.add_handler(CommandHandler("admin", admin_command))
        
        # Add callback query handler
        application.add_handler(CallbackQueryHandler(button_callback))
        
        # Add message handler for text messages
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
        
        # Add job for periodic cleanup of inactive games
        job_queue = application.job_queue
        job_queue.run_repeating(cleanup_inactive_games, interval=300, first=300)  # Run every 5 minutes
        
        # Start the Bot
        await application.initialize()
        await application.start()
        await application.updater.start_polling()
        
        logger.info("Bot started successfully")
        
        # Run the bot until the user presses Ctrl-C
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
    
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise

if __name__ == "__main__":
    asyncio.run(main())
