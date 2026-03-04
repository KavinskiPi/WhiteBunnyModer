import asyncio
import logging
import json
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set
from dataclasses import dataclass, asdict
from enum import IntEnum
from pathlib import Path

from telegram import Update, ChatPermissions, Chat
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.constants import ParseMode

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Классы ролей
class UserRole(IntEnum):
    BANNED = 0      # Забаненный пользователь
    USER = 1        # Обычный пользователь
    MODERATOR = 2   # Модератор (роль 1) - может выдавать 1 варн в 24 часа
    JUNIOR_ADMIN = 3 # Младший админ (роль 2) - мут 1 час, 2 варна в сутки
    SENIOR_ADMIN = 4 # Старший админ (роль 3) - бан раз в 3 дня, безлимитные варны/муты
    OWNER = 5       # Владелец (роль 4) - без лимитов
    
    @classmethod
    def get_role_name(cls, role: int) -> str:
        names = {
            0: "🚫 Забанен",
            1: "👤 Пользователь",
            2: "🛡 Модератор",
            3: "⚔ Младший админ",
            4: "👑 Старший админ",
            5: "👑 Владелец"
        }
        return names.get(role, "Неизвестно")

@dataclass
class UserData:
    user_id: int
    username: Optional[str]
    first_name: str
    last_name: Optional[str]
    role: int
    chat_id: int
    warnings_today: int = 0
    mutes_today: int = 0
    bans_count: int = 0
    last_warn_time: Optional[str] = None
    last_mute_time: Optional[str] = None
    last_ban_time: Optional[str] = None
    joined_at: Optional[str] = None

@dataclass
class Punishment:
    user_id: int
    user_name: str
    user_role_at_time: int
    type: str  # warn, mute, ban
    reason: str
    duration: Optional[str]
    admin_id: int
    admin_name: str
    admin_role_at_time: int
    timestamp: str
    expires_at: Optional[str]
    chat_id: int
    chat_title: str

class ModerationBot:
    def __init__(self, owner_username: str = "@WhiteBunnyHuh"):
        self.owner_username = owner_username.lower()  # Владелец по username
        self.owner_id: Optional[int] = None  # Будет установлен при первом входе владельца
        
        self.authorized_chats: Dict[int, dict] = {}  # {chat_id: {"title": str, "added_at": str}}
        self.chat_users: Dict[int, Dict[int, UserData]] = {}  # {chat_id: {user_id: UserData}}
        self.punishments: Dict[int, List[dict]] = {}  # {chat_id: [punishments]}
        
        # Настройки чатов
        self.chat_settings: Dict[int, dict] = {}  # chat_id -> settings
        
        # Файл для хранения данных
        self.data_file = "bot_data.json"
        self.load_data()
        
    def is_owner_by_username(self, username: Optional[str]) -> bool:
        """Проверка, является ли пользователь владельцем по username"""
        if not username:
            return False
        return f"@{username.lower()}" == self.owner_username
    
    def set_owner_id(self, user_id: int):
        """Установка ID владельца"""
        if self.owner_id is None:
            self.owner_id = user_id
            self.save_data()
            return True
        return False
    
    def get_user_role(self, chat_id: int, user_id: int) -> int:
        """Получение роли пользователя в чате"""
        # Владелец по username всегда имеет роль 5 в любом чате, где он есть
        if self.owner_id and user_id == self.owner_id:
            return UserRole.OWNER
        
        if chat_id in self.chat_users and user_id in self.chat_users[chat_id]:
            return self.chat_users[chat_id][user_id].role
        return UserRole.USER
    
    def update_user_role(self, chat_id: int, user_id: int, new_role: int, admin_id: int) -> bool:
        """Обновление роли пользователя"""
        if chat_id not in self.chat_users:
            self.chat_users[chat_id] = {}
        
        admin_role = self.get_user_role(chat_id, admin_id)
        
        # Проверка прав на изменение роли
        if admin_role <= new_role or admin_role <= self.get_user_role(chat_id, user_id):
            return False
        
        if user_id in self.chat_users[chat_id]:
            self.chat_users[chat_id][user_id].role = new_role
        else:
            # Если пользователь не найден, создаем запись
            self.chat_users[chat_id][user_id] = UserData(
                user_id=user_id,
                username=None,
                first_name="Unknown",
                last_name=None,
                role=new_role,
                chat_id=chat_id
            )
        
        self.save_data()
        return True
    
    def add_user_to_chat(self, chat_id: int, user) -> UserData:
        """Добавление пользователя в чат"""
        if chat_id not in self.chat_users:
            self.chat_users[chat_id] = {}
        
        user_id = user.id
        
        # Проверяем, не владелец ли это
        role = UserRole.OWNER if self.is_owner_by_username(user.username) else UserRole.USER
        
        if user_id not in self.chat_users[chat_id]:
            user_data = UserData(
                user_id=user_id,
                username=user.username,
                first_name=user.first_name,
                last_name=user.last_name,
                role=role,
                chat_id=chat_id,
                joined_at=datetime.now().isoformat()
            )
            self.chat_users[chat_id][user_id] = user_data
            
            # Если это владелец, сохраняем его ID
            if role == UserRole.OWNER and self.owner_id is None:
                self.set_owner_id(user_id)
            
            self.save_data()
            return user_data
        
        return self.chat_users[chat_id][user_id]
    
    def remove_user_from_chat(self, chat_id: int, user_id: int):
        """Удаление пользователя из чата"""
        if chat_id in self.chat_users and user_id in self.chat_users[chat_id]:
            del self.chat_users[chat_id][user_id]
            self.save_data()
    
    def check_permission(self, chat_id: int, admin_id: int, target_id: int, action: str) -> tuple[bool, str]:
        """Проверка прав на действие"""
        admin_role = self.get_user_role(chat_id, admin_id)
        target_role = self.get_user_role(chat_id, target_id)
        
        # Нельзя наказать пользователя с ролью выше или равной
        if admin_role <= target_role and admin_id != target_id:
            return False, f"❌ Нельзя наказать пользователя с ролью {UserRole.get_role_name(target_role)}"
        
        # Проверка лимитов для разных ролей
        if admin_id in self.chat_users[chat_id]:
            admin_data = self.chat_users[chat_id][admin_id]
            today = datetime.now().date().isoformat()
            
            if action == "warn":
                if admin_role == UserRole.MODERATOR:  # Роль 1
                    # Проверяем, был ли варн сегодня
                    if admin_data.last_warn_time and admin_data.last_warn_time.startswith(today):
                        return False, "❌ Модератор может выдавать только 1 варн в сутки"
                
                elif admin_role == UserRole.JUNIOR_ADMIN:  # Роль 2
                    if admin_data.warnings_today >= 2:
                        return False, "❌ Младший админ может выдавать только 2 варна в сутки"
                
                # Роль 3 и выше без лимита
            
            elif action == "mute":
                if admin_role == UserRole.JUNIOR_ADMIN:  # Роль 2
                    if admin_data.mutes_today >= 1:
                        return False, "❌ Младший админ может выдавать только 1 мут в сутки"
                    
                    # Проверяем длительность мута (макс 1 час)
                    return True, ""  # Длительность проверим отдельно
                
                # Роль 3 и выше без лимита
            
            elif action == "ban":
                if admin_role == UserRole.SENIOR_ADMIN:  # Роль 3
                    # Проверяем, был ли бан за последние 3 дня
                    if admin_data.last_ban_time:
                        last_ban = datetime.fromisoformat(admin_data.last_ban_time)
                        if datetime.now() - last_ban < timedelta(days=3):
                            return False, "❌ Старший админ может банить только раз в 3 дня"
        
        return True, ""
    
    def update_admin_stats(self, chat_id: int, admin_id: int, action: str, duration: Optional[str] = None):
        """Обновление статистики администратора"""
        if chat_id in self.chat_users and admin_id in self.chat_users[chat_id]:
            admin_data = self.chat_users[chat_id][admin_id]
            now = datetime.now().isoformat()
            
            if action == "warn":
                admin_data.warnings_today += 1
                admin_data.last_warn_time = now
            elif action == "mute":
                admin_data.mutes_today += 1
                admin_data.last_mute_time = now
            elif action == "ban":
                admin_data.bans_count += 1
                admin_data.last_ban_time = now
            
            self.save_data()
    
    def reset_daily_limits(self, chat_id: int):
        """Сброс дневных лимитов"""
        if chat_id in self.chat_users:
            today = datetime.now().date().isoformat()
            for user_id, user_data in self.chat_users[chat_id].items():
                if user_data.last_warn_time and not user_data.last_warn_time.startswith(today):
                    user_data.warnings_today = 0
                if user_data.last_mute_time and not user_data.last_mute_time.startswith(today):
                    user_data.mutes_today = 0
            self.save_data()
    
    def is_chat_authorized(self, chat_id: int) -> bool:
        """Проверка, авторизован ли чат"""
        return chat_id in self.authorized_chats
    
    def authorize_chat(self, chat_id: int, chat_title: str):
        """Авторизация чата"""
        self.authorized_chats[chat_id] = {
            "title": chat_title,
            "added_at": datetime.now().isoformat()
        }
        
        if chat_id not in self.chat_settings:
            self.chat_settings[chat_id] = {
                "max_warns_before_kick": 3,
                "default_mute_duration": "1ч",
                "default_ban_duration": "1д"
            }
        
        self.save_data()
    
    def remove_chat(self, chat_id: int):
        """Удаление чата"""
        if chat_id in self.authorized_chats:
            del self.authorized_chats[chat_id]
        if chat_id in self.chat_users:
            del self.chat_users[chat_id]
        if chat_id in self.punishments:
            del self.punishments[chat_id]
        if chat_id in self.chat_settings:
            del self.chat_settings[chat_id]
        self.save_data()
    
    def parse_duration(self, duration_str: str, admin_role: int = UserRole.OWNER) -> Optional[timedelta]:
        """Парсинг строки с длительностью с учетом ограничений по ролям"""
        try:
            value = int(duration_str[:-1])
            unit = duration_str[-1]
            
            # Ограничения для младшего админа (роль 2)
            if admin_role == UserRole.JUNIOR_ADMIN and unit == 'ч' and value > 1:
                return None  # Младший админ не может мутить больше 1 часа
            
            if unit == 'м':
                return timedelta(minutes=value)
            elif unit == 'ч':
                return timedelta(hours=value)
            elif unit == 'д':
                return timedelta(days=value)
            elif unit == 'н':
                return timedelta(weeks=value)
            else:
                return None
        except:
            return None
    
    def get_user_mention(self, user) -> str:
        """Получение упоминания пользователя"""
        if user.username:
            return f"@{user.username}"
        else:
            return f"<a href='tg://user?id={user.id}'>{user.full_name}</a>"
    
    def save_data(self):
        """Сохранение данных в файл"""
        data = {
            "owner_id": self.owner_id,
            "owner_username": self.owner_username,
            "authorized_chats": self.authorized_chats,
            "chat_settings": self.chat_settings,
            "punishments": self.punishments,
            "chat_users": {
                str(chat_id): {
                    str(user_id): asdict(user_data)
                    for user_id, user_data in users.items()
                }
                for chat_id, users in self.chat_users.items()
            }
        }
        
        try:
            with open(self.data_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Ошибка при сохранении данных: {e}")
    
    def load_data(self):
        """Загрузка данных из файла"""
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.owner_id = data.get("owner_id")
                    self.owner_username = data.get("owner_username", "@WhiteBunnyHuh")
                    self.authorized_chats = {int(k): v for k, v in data.get("authorized_chats", {}).items()}
                    self.chat_settings = {int(k): v for k, v in data.get("chat_settings", {}).items()}
                    self.punishments = {int(k): v for k, v in data.get("punishments", {}).items()}
                    
                    # Загрузка пользователей
                    self.chat_users = {}
                    for chat_id_str, users_data in data.get("chat_users", {}).items():
                        chat_id = int(chat_id_str)
                        self.chat_users[chat_id] = {}
                        for user_id_str, user_dict in users_data.items():
                            user_id = int(user_id_str)
                            self.chat_users[chat_id][user_id] = UserData(**user_dict)
            except Exception as e:
                logger.error(f"Ошибка при загрузке данных: {e}")

# Глобальная переменная бота
mod_bot = ModerationBot()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user = update.effective_user
    chat = update.effective_chat
    
    # Добавляем пользователя в базу если это личный чат
    if chat.type == "private":
        await update.message.reply_text(
            "👋 Привет! Я бот для модерации групп.\n\n"
            "Меня может добавлять в группы только @WhiteBunnyHuh\n"
            "В группах у каждого пользователя есть роль, которая определяет его права."
        )
    else:
        # В группе показываем информацию о ролях
        user_role = mod_bot.get_user_role(chat.id, user.id)
        await update.message.reply_text(
            f"👋 Привет, {user.first_name}!\n"
            f"Твоя роль в этом чате: {UserRole.get_role_name(user_role)}\n\n"
            f"📋 Доступные команды:\n"
            f"/role [@username] - узнать роль пользователя\n"
            f"/admins - список администраторов чата\n"
            f"/help - помощь по командам"
        )

async def handle_new_chat_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик добавления новых участников"""
    chat = update.effective_chat
    
    for member in update.message.new_chat_members:
        if member.id == context.bot.id:
            # Бота добавили в чат
            added_by = update.message.from_user
            
            if mod_bot.is_owner_by_username(added_by.username):
                # Владелец добавил бота
                mod_bot.authorize_chat(chat.id, chat.title)
                await update.message.reply_text(
                    f"✅ Бот авторизован в чате!\n"
                    f"Теперь @WhiteBunnyHuh является Владельцем бота в этом чате.\n"
                    f"Используйте /help для списка команд."
                )
            else:
                # Не владелец добавил бота - выходим
                await update.message.reply_text(
                    "❌ Только @WhiteBunnyHuh может добавлять меня в группы.\n"
                    "Я покидаю чат."
                )
                await context.bot.leave_chat(chat.id)
                return
        else:
            # Добавили обычного пользователя
            mod_bot.add_user_to_chat(chat.id, member)

async def handle_left_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик выхода участников"""
    chat = update.effective_chat
    left_member = update.message.left_chat_member
    
    mod_bot.remove_user_from_chat(chat.id, left_member.id)

async def check_owner_presence(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверка присутствия владельца в чате"""
    chat = update.effective_chat
    
    # Проверяем, есть ли владелец в чате
    owner_in_chat = False
    try:
        if mod_bot.owner_id:
            member = await context.bot.get_chat_member(chat.id, mod_bot.owner_id)
            owner_in_chat = True
    except:
        pass
    
    # Если владельца нет в чате, бот должен выйти
    if not owner_in_chat and mod_bot.is_chat_authorized(chat.id):
        await update.message.reply_text(
            "❌ @WhiteBunnyHuh больше нет в этом чате.\n"
            "Я покидаю чат, так как только он может меня добавлять."
        )
        await context.bot.leave_chat(chat.id)
        mod_bot.remove_chat(chat.id)
        return False
    
    return True

async def role_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /role - узнать роль пользователя"""
    chat = update.effective_chat
    
    if not mod_bot.is_chat_authorized(chat.id):
        return
    
    # Проверяем присутствие владельца
    if not await check_owner_presence(update, context):
        return
    
    target = None
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
    elif context.args:
        username = context.args[0].replace('@', '')
        # Ищем пользователя по username
        for user_id, user_data in mod_bot.chat_users.get(chat.id, {}).items():
            if user_data.username and user_data.username.lower() == username.lower():
                target = user_data
                break
    else:
        target = update.effective_user
    
    if target:
        user_id = target.id if hasattr(target, 'id') else target.user_id
        user_name = target.first_name if hasattr(target, 'first_name') else target.first_name
        role = mod_bot.get_user_role(chat.id, user_id)
        
        await update.message.reply_text(
            f"👤 Пользователь: {user_name}\n"
            f"📊 Роль: {UserRole.get_role_name(role)}"
        )
    else:
        await update.message.reply_text("❌ Пользователь не найден")

async def admins_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /admins - список администраторов чата"""
    chat = update.effective_chat
    
    if not mod_bot.is_chat_authorized(chat.id):
        return
    
    if not await check_owner_presence(update, context):
        return
    
    admins_list = []
    if chat.id in mod_bot.chat_users:
        for user_id, user_data in mod_bot.chat_users[chat.id].items():
            if user_data.role >= UserRole.MODERATOR:
                mention = f"@{user_data.username}" if user_data.username else user_data.first_name
                admins_list.append(f"• {mention} - {UserRole.get_role_name(user_data.role)}")
    
    if admins_list:
        response = "👑 **Администраторы чата:**\n\n" + "\n".join(admins_list)
    else:
        response = "📋 В этом чате нет администраторов"
    
    await update.message.reply_text(response, parse_mode=ParseMode.MARKDOWN)

async def demote_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда Понизить - понижение роли пользователя (только для @WhiteBunnyHuh)"""
    user = update.effective_user
    
    # Проверяем, что команду дает @WhiteBunnyHuh
    if not mod_bot.is_owner_by_username(user.username):
        await update.message.reply_text("❌ Только @WhiteBunnyHuh может использовать эту команду")
        return
    
    chat = update.effective_chat
    
    if not mod_bot.is_chat_authorized(chat.id):
        await update.message.reply_text("❌ Чат не авторизован")
        return
    
    # Проверяем формат команды
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "❌ Использование: Понизить @username [роль]\n"
            "Роли: 1 - Модератор, 2 - Мл.админ, 3 - Ст.админ, 4 - Пользователь"
        )
        return
    
    target_username = context.args[0].replace('@', '')
    try:
        new_role = int(context.args[1])
        if new_role not in [1, 2, 3, 4]:
            raise ValueError
        # Конвертируем в значение UserRole
        if new_role == 4:
            new_role = UserRole.USER
        elif new_role == 1:
            new_role = UserRole.MODERATOR
        elif new_role == 2:
            new_role = UserRole.JUNIOR_ADMIN
        elif new_role == 3:
            new_role = UserRole.SENIOR_ADMIN
    except:
        await update.message.reply_text("❌ Некорректная роль. Используйте 1, 2, 3 или 4")
        return
    
    # Ищем пользователя
    target_user_id = None
    target_user_name = None
    for uid, udata in mod_bot.chat_users.get(chat.id, {}).items():
        if udata.username and udata.username.lower() == target_username.lower():
            target_user_id = uid
            target_user_name = udata.first_name
            break
    
    if not target_user_id:
        await update.message.reply_text(f"❌ Пользователь @{target_username} не найден в этом чате")
        return
    
    # Понижаем роль
    if mod_bot.update_user_role(chat.id, target_user_id, new_role, user.id):
        role_name = UserRole.get_role_name(new_role)
        await update.message.reply_text(
            f"✅ Роль пользователя @{target_username} изменена на {role_name}"
        )
    else:
        await update.message.reply_text("❌ Не удалось изменить роль")

async def promote_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /promote - повышение роли (только для владельца)"""
    user = update.effective_user
    
    if not mod_bot.is_owner_by_username(user.username):
        await update.message.reply_text("❌ Только @WhiteBunnyHuh может использовать эту команду")
        return
    
    chat = update.effective_chat
    
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Использование: /promote @username [роль]")
        return
    
    target_username = context.args[0].replace('@', '')
    try:
        new_role = int(context.args[1])
        if new_role not in [1, 2, 3]:
            raise ValueError
        # Конвертируем в значение UserRole
        if new_role == 1:
            new_role = UserRole.MODERATOR
        elif new_role == 2:
            new_role = UserRole.JUNIOR_ADMIN
        elif new_role == 3:
            new_role = UserRole.SENIOR_ADMIN
    except:
        await update.message.reply_text("❌ Некорректная роль. Используйте 1, 2 или 3")
        return
    
    # Поиск пользователя
    target_user_id = None
    for uid, udata in mod_bot.chat_users.get(chat.id, {}).items():
        if udata.username and udata.username.lower() == target_username.lower():
            target_user_id = uid
            break
    
    if not target_user_id:
        await update.message.reply_text(f"❌ Пользователь @{target_username} не найден")
        return
    
    if mod_bot.update_user_role(chat.id, target_user_id, new_role, user.id):
        role_name = UserRole.get_role_name(new_role)
        await update.message.reply_text(f"✅ Пользователь @{target_username} повышен до {role_name}")
    else:
        await update.message.reply_text("❌ Не удалось повысить пользователя")

async def warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /warn - выдать предупреждение"""
    chat = update.effective_chat
    
    if not mod_bot.is_chat_authorized(chat.id):
        return
    
    if not await check_owner_presence(update, context):
        return
    
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ Ответьте на сообщение пользователя")
        return
    
    admin = update.effective_user
    target = update.message.reply_to_message.from_user
    reason = ' '.join(context.args) if context.args else "Без причины"
    
    # Проверяем права
    has_perm, msg = mod_bot.check_permission(chat.id, admin.id, target.id, "warn")
    if not has_perm:
        await update.message.reply_text(msg)
        return
    
    # Создаем запись о наказании
    punishment = {
        "user_id": target.id,
        "user_name": target.full_name,
        "user_role_at_time": mod_bot.get_user_role(chat.id, target.id),
        "type": "warn",
        "reason": reason,
        "duration": None,
        "admin_id": admin.id,
        "admin_name": admin.full_name,
        "admin_role_at_time": mod_bot.get_user_role(chat.id, admin.id),
        "timestamp": datetime.now().isoformat(),
        "expires_at": None,
        "chat_id": chat.id,
        "chat_title": chat.title
    }
    
    # Сохраняем
    if chat.id not in mod_bot.punishments:
        mod_bot.punishments[chat.id] = []
    mod_bot.punishments[chat.id].append(punishment)
    
    # Обновляем статистику админа
    mod_bot.update_admin_stats(chat.id, admin.id, "warn")
    
    admin_mention = mod_bot.get_user_mention(admin)
    target_mention = mod_bot.get_user_mention(target)
    
    await update.message.reply_text(
        f"⚠️ Предупреждение выдано {target_mention}\n"
        f"👮 Администратор: {admin_mention}\n"
        f"📝 Причина: {reason}",
        parse_mode=ParseMode.HTML
    )
    
    mod_bot.save_data()

async def mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /mute - мут пользователя"""
    chat = update.effective_chat
    
    if not mod_bot.is_chat_authorized(chat.id):
        return
    
    if not await check_owner_presence(update, context):
        return
    
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ Ответьте на сообщение пользователя")
        return
    
    admin = update.effective_user
    target = update.message.reply_to_message.from_user
    args = context.args
    
    admin_role = mod_bot.get_user_role(chat.id, admin.id)
    
    # Парсим аргументы
    duration_str = "1ч"  # По умолчанию
    reason = "Без причины"
    
    if args:
        possible_duration = mod_bot.parse_duration(args[0], admin_role)
        if possible_duration:
            duration_str = args[0]
            reason = ' '.join(args[1:]) if len(args) > 1 else "Без причины"
        else:
            reason = ' '.join(args)
    
    # Проверяем права
    has_perm, msg = mod_bot.check_permission(chat.id, admin.id, target.id, "mute")
    if not has_perm:
        await update.message.reply_text(msg)
        return
    
    duration = mod_bot.parse_duration(duration_str, admin_role)
    if not duration:
        await update.message.reply_text("❌ Некорректный формат времени или превышен лимит")
        return
    
    try:
        until_date = datetime.now() + duration
        
        permissions = ChatPermissions(can_send_messages=False)
        await context.bot.restrict_chat_member(
            chat_id=chat.id,
            user_id=target.id,
            permissions=permissions,
            until_date=until_date
        )
        
        punishment = {
            "user_id": target.id,
            "user_name": target.full_name,
            "user_role_at_time": mod_bot.get_user_role(chat.id, target.id),
            "type": "mute",
            "reason": reason,
            "duration": duration_str,
            "admin_id": admin.id,
            "admin_name": admin.full_name,
            "admin_role_at_time": admin_role,
            "timestamp": datetime.now().isoformat(),
            "expires_at": until_date.isoformat(),
            "chat_id": chat.id,
            "chat_title": chat.title
        }
        
        if chat.id not in mod_bot.punishments:
            mod_bot.punishments[chat.id] = []
        mod_bot.punishments[chat.id].append(punishment)
        
        # Обновляем статистику админа
        mod_bot.update_admin_stats(chat.id, admin.id, "mute")
        
        admin_mention = mod_bot.get_user_mention(admin)
        target_mention = mod_bot.get_user_mention(target)
        
        await update.message.reply_text(
            f"🔇 Пользователь {target_mention} замучен\n"
            f"👮 Администратор: {admin_mention}\n"
            f"⏱ Длительность: {duration_str}\n"
            f"📝 Причина: {reason}",
            parse_mode=ParseMode.HTML
        )
        
        mod_bot.save_data()
        
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")

async def ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /ban - бан пользователя"""
    chat = update.effective_chat
    
    if not mod_bot.is_chat_authorized(chat.id):
        return
    
    if not await check_owner_presence(update, context):
        return
    
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ Ответьте на сообщение пользователя")
        return
    
    admin = update.effective_user
    target = update.message.reply_to_message.from_user
    args = context.args
    
    admin_role = mod_bot.get_user_role(chat.id, admin.id)
    
    # Парсим аргументы
    duration_str = "1д"  # По умолчанию
    reason = "Без причины"
    
    if args:
        possible_duration = mod_bot.parse_duration(args[0], admin_role)
        if possible_duration:
            duration_str = args[0]
            reason = ' '.join(args[1:]) if len(args) > 1 else "Без причины"
        else:
            reason = ' '.join(args)
    
    # Проверяем права
    has_perm, msg = mod_bot.check_permission(chat.id, admin.id, target.id, "ban")
    if not has_perm:
        await update.message.reply_text(msg)
        return
    
    duration = mod_bot.parse_duration(duration_str, admin_role)
    if not duration:
        await update.message.reply_text("❌ Некорректный формат времени")
        return
    
    try:
        until_date = datetime.now() + duration
        
        await context.bot.ban_chat_member(
            chat_id=chat.id,
            user_id=target.id,
            until_date=until_date
        )
        
        punishment = {
            "user_id": target.id,
            "user_name": target.full_name,
            "user_role_at_time": mod_bot.get_user_role(chat.id, target.id),
            "type": "ban",
            "reason": reason,
            "duration": duration_str,
            "admin_id": admin.id,
            "admin_name": admin.full_name,
            "admin_role_at_time": admin_role,
            "timestamp": datetime.now().isoformat(),
            "expires_at": until_date.isoformat(),
            "chat_id": chat.id,
            "chat_title": chat.title
        }
        
        if chat.id not in mod_bot.punishments:
            mod_bot.punishments[chat.id] = []
        mod_bot.punishments[chat.id].append(punishment)
        
        # Обновляем статистику админа
        mod_bot.update_admin_stats(chat.id, admin.id, "ban")
        
        admin_mention = mod_bot.get_user_mention(admin)
        target_mention = mod_bot.get_user_mention(target)
        
        await update.message.reply_text(
            f"✅ Пользователь {target_mention} забанен\n"
            f"👮 Администратор: {admin_mention}\n"
            f"⏱ Длительность: {duration_str}\n"
            f"📝 Причина: {reason}",
            parse_mode=ParseMode.HTML
        )
        
        mod_bot.save_data()
        
        # Удаляем пользователя из базы
        mod_bot.remove_user_from_chat(chat.id, target.id)
        
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")

async def unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /unban - разбан пользователя"""
    chat = update.effective_chat
    
    if not mod_bot.is_chat_authorized(chat.id):
        return
    
    if not await check_owner_presence(update, context):
        return
    
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ Ответьте на сообщение пользователя")
        return
    
    admin = update.effective_user
    target = update.message.reply_to_message.from_user
    
    try:
        await context.bot.unban_chat_member(
            chat_id=chat.id,
            user_id=target.id
        )
        
        admin_mention = mod_bot.get_user_mention(admin)
        target_mention = mod_bot.get_user_mention(target)
        
        await update.message.reply_text(
            f"✅ Пользователь {target_mention} разбанен\n"
            f"👮 Администратор: {admin_mention}",
            parse_mode=ParseMode.HTML
        )
        
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")

async def punishments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /punishments - показать наказания"""
    chat = update.effective_chat
    
    if not mod_bot.is_chat_authorized(chat.id):
        return
    
    if not await check_owner_presence(update, context):
        return
    
    if chat.id not in mod_bot.punishments or not mod_bot.punishments[chat.id]:
        await update.message.reply_text("📊 Нет записей о наказаниях")
        return
    
    response = "📊 **Последние наказания:**\n\n"
    count = 0
    
    for p in reversed(mod_bot.punishments[chat.id][-10:]):  # Последние 10
        response += f"• {p['type'].upper()}: {p['user_name']}\n"
        response += f"  Причина: {p['reason']}\n"
        response += f"  Админ: {p['admin_name']}\n"
        response += f"  Дата: {p['timestamp'][:16]}\n\n"
        count += 1
    
    await update.message.reply_text(response, parse_mode=ParseMode.MARKDOWN)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /help - справка"""
    chat = update.effective_chat
    user = update.effective_user
    
    if not mod_bot.is_chat_authorized(chat.id):
        return
    
    user_role = mod_bot.get_user_role(chat.id, user.id)
    
    help_text = f"📋 **Доступные команды для {UserRole.get_role_name(user_role)}:**\n\n"
    
    # Базовые команды для всех
    help_text += "👤 **Общие команды:**\n"
    help_text += "/role [@username] - узнать роль пользователя\n"
    help_text += "/admins - список администраторов\n"
    help_text += "/punishments - последние наказания\n\n"
    
    # Команды для модераторов и выше
    if user_role >= UserRole.MODERATOR:
        help_text += "🛡 **Команды модерации:**\n"
        help_text += "/warn [причина] - выдать предупреждение\n"
        
    if user_role >= UserRole.JUNIOR_ADMIN:
        help_text += "/mute [время] [причина] - замутить (макс 1ч для роли 2)\n"
        
    if user_role >= UserRole.SENIOR_ADMIN:
        help_text += "/ban [время] [причина] - забанить (раз в 3 дня для роли 3)\n"
        
    if user_role >= UserRole.OWNER:
        help_text += "\n👑 **Команды владельца:**\n"
        help_text += "/promote @username [роль] - повысить пользователя\n"
        help_text += "/demote @username [роль] - понизить пользователя\n"
        help_text += "Или используйте: Понизить @username [роль]\n\n"
    
    help_text += "**Форматы времени:** 30м, 1ч, 2д, 1н"
    
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

def main():
    """Основная функция запуска бота"""
    token = "YOUR_BOT_TOKEN_HERE"  # Замените на ваш токен
    
    application = Application.builder().token(token).build()
    
    # Обработчики команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("role", role_command))
    application.add_handler(CommandHandler("admins", admins_command))
    application.add_handler(CommandHandler("promote", promote_command))
    application.add_handler(CommandHandler("punishments", punishments))
    
    # Команды модерации
    application.add_handler(CommandHandler("warn", warn))
    application.add_handler(CommandHandler("mute", mute))
    application.add_handler(CommandHandler("ban", ban))
    application.add_handler(CommandHandler("unban", unban))
    
    # Обработчик для команды "Понизить" (текстовая команда)
    application.add_handler(MessageHandler(
        filters.Regex(r'^Понизить\s+@(\w+)\s+(\d+)$') & filters.ChatType.GROUPS,
        demote_command
    ))
    
    # Обработчики событий чата
    application.add_handler(MessageHandler(
        filters.StatusUpdate.NEW_CHAT_MEMBERS, 
        handle_new_chat_members
    ))
    application.add_handler(MessageHandler(
        filters.StatusUpdate.LEFT_CHAT_MEMBER, 
        handle_left_chat_member
    ))
    
    # Периодическая задача для сброса дневных лимитов
    async def reset_daily_limits_callback(context: ContextTypes.DEFAULT_TYPE):
        for chat_id in mod_bot.authorized_chats:
            mod_bot.reset_daily_limits(chat_id)
    
    job_queue = application.job_queue
    if job_queue:
        job_queue.run_daily(reset_daily_limits_callback, time=datetime.time(hour=0, minute=0))
    
    print("🤖 Бот запущен...")
    print(f"👑 Владелец бота: {mod_bot.owner_username}")
    print("Ожидание команд...")
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
