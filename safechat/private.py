"""Личный чат с медиатором: приветствие, «Как это работает», подключение чатов, «Мои чаты», паспорт чата."""

import logging

from telegram import InlineKeyboardButton as Button
from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from safechat import db, texts

logger = logging.getLogger(__name__)

HTML = ParseMode.HTML
SENSITIVITY_ORDER = ["low", "medium", "high"]


# ---------- Клавиатуры ----------

def main_menu(has_chats: bool) -> InlineKeyboardMarkup:
    rows = [[Button("➕ Подключить чат", callback_data="m:connect")]]
    if has_chats:
        rows.append([Button("💬 Мои чаты", callback_data="m:chats")])
        rows.append([Button("📊 Статистика", callback_data="stats:all")])
    rows.append([Button("📖 Как это работает", callback_data="m:how:0")])
    return InlineKeyboardMarkup(rows)


def how_page(page: int) -> tuple[str, InlineKeyboardMarkup]:
    last = len(texts.HOW_IT_WORKS) - 1
    nav = []
    if page > 0:
        nav.append(Button("← Назад", callback_data=f"m:how:{page - 1}"))
    if page < last:
        nav.append(Button("Далее →", callback_data=f"m:how:{page + 1}"))
    rows = [nav]
    if page == last:
        rows.append([Button("➕ Подключить чат", callback_data="m:connect")])
    rows.append([Button("🏠 В меню", callback_data="m:home")])
    text = f"{texts.HOW_IT_WORKS[page]}\n\n<i>{page + 1} из {last + 1}</i>"
    return text, InlineKeyboardMarkup(rows)


def passport_offer(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [Button("📝 Заполнить паспорт чата", callback_data=f"m:pass:{chat_id}")],
        [Button("Позже", callback_data="m:later")],
    ])


def kinds_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    rows = [[Button(label, callback_data=f"m:kind:{chat_id}:{key}")] for key, label in texts.CHAT_KINDS.items()]
    rows.append([Button("Пропустить", callback_data=f"m:kind:{chat_id}:-")])
    return InlineKeyboardMarkup(rows)


def skip_keyboard(chat_id: int, step: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[Button("Пропустить", callback_data=f"m:skip:{chat_id}:{step}")]])


def chat_card_keyboard(chat: db.Chat) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [Button("📊 Статистика", callback_data=f"stats:{chat.id}")],
        [Button("📝 Паспорт чата", callback_data=f"m:pass:{chat.id}")],
        [Button(f"🎚 Чувствительность: {texts.SENSITIVITY[chat.sensitivity]}", callback_data=f"m:sens:{chat.id}")],
        [Button("🔌 Отключить чат", callback_data=f"m:disc:{chat.id}")],
        [Button("← Мои чаты", callback_data="m:chats")],
    ])


async def chats_view(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    chats = await db.mediated_chats(user_id)
    rows = [[Button(f"💬 {c.title or 'Без названия'}", callback_data=f"m:chat:{c.id}")] for c in chats]
    rows.append([Button("➕ Подключить чат", callback_data="m:connect")])
    rows.append([Button("🏠 В меню", callback_data="m:home")])
    return (texts.MY_CHATS if chats else texts.MY_CHATS_EMPTY), InlineKeyboardMarkup(rows)


async def card_view(chat: db.Chat) -> tuple[str, InlineKeyboardMarkup]:
    messages, incidents = await db.chat_counters(chat.id)
    return texts.chat_card(chat, messages, incidents), chat_card_keyboard(chat)


async def own_chat(user_id: int, chat_id: int) -> db.Chat | None:
    chat = await db.get_chat(chat_id)
    return chat if chat and chat.active and chat.mediator_id == user_id else None


# ---------- Команды ----------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await db.upsert_user(user.id, user.full_name, user.username, started=True)
    await db.set_user_state(user.id, None)

    # Чаты, которые человек подключил, ещё не открыв бота: сообщаем о них вместо обычного приветствия.
    pending = await db.unnotified_chats(user.id)
    for chat in pending:
        await update.message.reply_text(texts.connected_pending(user.first_name, chat.title),
                                        parse_mode=HTML, reply_markup=passport_offer(chat.id))
        await db.update_chat(chat.id, mediator_notified=True)
    if pending:
        return

    chats = await db.mediated_chats(user.id)
    await update.message.reply_text(texts.welcome(user.first_name), parse_mode=HTML, reply_markup=main_menu(bool(chats)))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, keyboard = how_page(0)
    await update.message.reply_text(text, parse_mode=HTML, reply_markup=keyboard)


async def cmd_chats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, keyboard = await chats_view(update.effective_user.id)
    await update.message.reply_text(text, parse_mode=HTML, reply_markup=keyboard)


# ---------- Кнопки меню ----------

async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user
    parts = query.data.split(":")
    action = parts[1]

    async def show(text: str, keyboard: InlineKeyboardMarkup | None = None) -> None:
        try:
            await query.edit_message_text(text, parse_mode=HTML, reply_markup=keyboard)
        except BadRequest as err:
            if "not modified" not in str(err).lower():
                raise

    if action == "home":
        await query.answer()
        await show(texts.welcome(user.first_name), main_menu(bool(await db.mediated_chats(user.id))))
    elif action == "how":
        await query.answer()
        await show(*how_page(int(parts[2])))
    elif action == "connect":
        await query.answer()
        url = f"https://t.me/{context.bot.username}?startgroup=connect"
        await show(texts.CONNECT, InlineKeyboardMarkup([
            [Button("👥 Выбрать группу", url=url)],
            [Button("← Назад", callback_data="m:home")],
        ]))
    elif action == "chats":
        await query.answer()
        await show(*await chats_view(user.id))
    elif action == "later":
        await query.answer()
        await query.edit_message_reply_markup(None)
        await query.message.reply_text("Хорошо! Заполнить паспорт можно позже в разделе «Мои чаты».",
                                       reply_markup=main_menu(True))
    else:
        await on_chat_action(update, context, action, parts[2:], show)


async def on_chat_action(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str, args: list[str], show) -> None:
    """Действия с конкретным чатом — только для его медиатора."""
    query = update.callback_query
    user = query.from_user
    chat = await own_chat(user.id, int(args[0]))
    if chat is None:
        await query.answer("Этот чат недоступен", show_alert=True)
        return
    await query.answer()

    if action == "chat":
        await show(*await card_view(chat))
    elif action == "sens":
        current = SENSITIVITY_ORDER.index(chat.sensitivity) if chat.sensitivity in SENSITIVITY_ORDER else 1
        chat.sensitivity = SENSITIVITY_ORDER[(current + 1) % len(SENSITIVITY_ORDER)]
        await db.update_chat(chat.id, sensitivity=chat.sensitivity)
        await show(*await card_view(chat))
    elif action == "disc":
        await show(texts.disconnect_confirm(chat.title), InlineKeyboardMarkup([
            [Button("🔌 Да, отключить", callback_data=f"m:discyes:{chat.id}")],
            [Button("Отмена", callback_data=f"m:chat:{chat.id}")],
        ]))
    elif action == "discyes":
        await db.disconnect_chat(chat.id)  # до выхода — чтобы не прислать «меня удалили»
        try:
            await context.bot.leave_chat(chat.id)
        except Exception:
            logger.warning("Не удалось выйти из чата %s", chat.id, exc_info=True)
        await show(texts.disconnected(chat.title), InlineKeyboardMarkup([[Button("← Мои чаты", callback_data="m:chats")]]))
    elif action == "pass":
        await query.edit_message_reply_markup(None)
        await query.message.reply_text(texts.passport_kind(chat.title), parse_mode=HTML,
                                       reply_markup=kinds_keyboard(chat.id))
    elif action == "kind":
        if args[1] in texts.CHAT_KINDS:
            await db.update_chat(chat.id, kind=args[1])
        await db.set_user_state(user.id, f"purpose:{chat.id}")
        await show(texts.passport_purpose(chat.title), skip_keyboard(chat.id, "purpose"))
    elif action == "skip":
        await query.edit_message_reply_markup(None)
        await next_passport_step(update, chat, after=args[1])


async def next_passport_step(update: Update, chat: db.Chat, after: str) -> None:
    user_id = update.effective_user.id
    message = update.effective_message
    if after == "purpose":
        await db.set_user_state(user_id, f"norms:{chat.id}")
        await message.reply_text(texts.passport_norms(chat.title), parse_mode=HTML,
                                 reply_markup=skip_keyboard(chat.id, "norms"))
    else:
        await db.set_user_state(user_id, None)
        await message.reply_text(texts.passport_done(chat.title), parse_mode=HTML, reply_markup=InlineKeyboardMarkup(
            [[Button("💬 Мои чаты", callback_data="m:chats")]]))


# ---------- Свободный текст ----------

async def on_private_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    db_user = await db.get_user(user.id)
    state = db_user.state if db_user else None
    if not state:
        await update.message.reply_text("Я пока понимаю кнопки и команды. Откройте меню: /start")
        return

    step, chat_id = state.split(":")
    chat = await own_chat(user.id, int(chat_id))
    if chat is None:
        await db.set_user_state(user.id, None)
        await update.message.reply_text("Этот чат больше недоступен. Откройте меню: /start")
        return

    answer = update.message.text.strip()[:1000]
    await db.update_chat(chat.id, **{step: answer})
    await next_passport_step(update, chat, after=step)
