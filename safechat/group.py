"""События в группах: подключение и отключение бота, сообщения, правки, смена ID группы."""

import asyncio
import logging
from datetime import timedelta

from telegram import Bot, Chat, ChatMember, Update, User
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from safechat import db, safety, texts
from safechat.monitor import ChatMonitor
from safechat.private import passport_offer

logger = logging.getLogger(__name__)

GROUP_TYPES = (Chat.GROUP, Chat.SUPERGROUP)
RECENT = timedelta(minutes=1)  # подключение «только что» — чтобы не обрабатывать его дважды

# При добавлении по ссылке Telegram присылает два события почти одновременно
# (бот добавлен + команда /start в группе). Блокировка не даёт подключить чат дважды.
_connect_locks: dict[int, asyncio.Lock] = {}


def is_in_chat(member: ChatMember) -> bool:
    if member.status in (ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER):
        return True
    return member.status == ChatMember.RESTRICTED and bool(getattr(member, "is_member", False))


async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception:
        return False
    return member.status in (ChatMember.OWNER, ChatMember.ADMINISTRATOR)


async def try_send(bot: Bot, chat_id: int, text: str, **kwargs) -> bool:
    try:
        await bot.send_message(chat_id, text[:4096], parse_mode=ParseMode.HTML, **kwargs)
        return True
    except Exception:
        return False


async def notify_connected(bot: Bot, chat: db.Chat) -> None:
    """Сообщает медиатору о подключении. Если он ещё не открывал бота — сообщим при первом /start."""
    if await try_send(bot, chat.mediator_id, texts.connected(chat.title), reply_markup=passport_offer(chat.id)):
        await db.update_chat(chat.id, mediator_notified=True)


async def connect(bot: Bot, chat: Chat, user: User) -> None:
    """Подключение чата админом: при добавлении бота или по команде /start в группе."""
    async with _connect_locks.setdefault(chat.id, asyncio.Lock()):
        await db.upsert_user(user.id, user.full_name, user.username)
        title = chat.title or ""

        if not await is_admin(bot, chat.id, user.id):
            await try_send(bot, user.id, texts.not_admin(title))
            try:
                await bot.leave_chat(chat.id)
            except Exception:
                logger.warning("Не удалось выйти из чата %s", chat.id, exc_info=True)
            return

        existing = await db.get_chat(chat.id)
        if existing and existing.active:
            if existing.mediator_id == user.id:
                if db.utcnow() - existing.connected_at > RECENT:
                    await try_send(bot, user.id, texts.already_yours(title))
                return  # иначе это второе событие того же подключения
            if existing.mediator_id and await is_admin(bot, chat.id, existing.mediator_id):
                await try_send(bot, user.id, texts.taken_for_new(title))
                await try_send(bot, existing.mediator_id, texts.taken_for_current(user.full_name, title))
                return
            # Медиатора нет или он больше не админ — роль переходит к подключающему.
            old_mediator = existing.mediator_id
            row = await db.connect_chat(chat.id, title, user.id)
            if old_mediator:
                await try_send(bot, old_mediator, texts.mediator_replaced(title))
            await notify_connected(bot, row)
            return

        row = await db.connect_chat(chat.id, title, user.id)
        await try_send(bot, chat.id, texts.GROUP_ANNOUNCEMENT)
        await notify_connected(bot, row)
        logger.info("Чат %s подключён, медиатор %s", chat.id, user.id)


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    change = update.my_chat_member
    chat = change.chat
    was_in, is_in = is_in_chat(change.old_chat_member), is_in_chat(change.new_chat_member)

    if chat.type == Chat.CHANNEL:
        if is_in:
            await context.bot.leave_chat(chat.id)  # каналы не поддерживаем
        return
    if chat.type not in GROUP_TYPES:
        return

    if is_in and not was_in:
        await connect(context.bot, chat, change.from_user)
    elif was_in and not is_in:
        row = await db.disconnect_chat(chat.id)
        if row and row.mediator_id:
            await try_send(context.bot, row.mediator_id, texts.removed(row.title))
        logger.info("Бота удалили из чата %s", chat.id)


async def on_group_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start в группе приходит, когда админ подключает чат по ссылке, а бот уже в группе."""
    if not await is_admin(context.bot, update.effective_chat.id, update.effective_user.id):
        return
    await connect(context.bot, update.effective_chat, update.effective_user)


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = message.from_user
    text = message.text or message.caption
    if user is None or user.is_bot or not text:
        return

    reply = message.reply_to_message
    saved = await db.save_message(
        chat_id=message.chat.id,
        chat_title=message.chat.title or "",
        tg_message_id=message.message_id,
        user_id=user.id,
        name=user.full_name,
        username=user.username,
        reply_to_name=reply.from_user.full_name if reply and reply.from_user else None,
        text=text,
    )
    if saved is None:
        return  # чат не подключён

    monitor: ChatMonitor = context.bot_data["monitor"]
    signal = safety.detect(text)
    if signal:
        monitor.on_safety(message.chat.id, signal, user.full_name, text)
    monitor.on_message(message.chat.id)


async def on_group_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.edited_message
    text = message.text or message.caption
    if text:
        await db.update_message_text(message.chat.id, message.message_id, text)


async def on_migrate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Группа стала супергруппой — у неё новый ID."""
    message = update.message
    if message.migrate_to_chat_id:
        await db.migrate_chat(message.chat.id, message.migrate_to_chat_id)


async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = await db.get_chat(update.message.chat.id)
    if chat is None or not chat.active:
        return
    for user in update.message.new_chat_members:
        if not user.is_bot:
            await db.touch_member(chat.id, user.id, user.full_name, user.username)


async def on_left_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.message.left_chat_member
    chat = await db.get_chat(update.message.chat.id)
    if chat and chat.active and user and chat.mediator_id == user.id:
        await db.update_chat(chat.id, mediator_id=None, mediator_notified=False)
        await try_send(context.bot, user.id, texts.mediator_left(chat.title))
