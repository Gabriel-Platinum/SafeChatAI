"""Команды бота и приём сообщений."""

import logging
from collections import Counter
from itertools import combinations

from telegram import Bot, ChatMember, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, ChatMemberHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from safechat import ai, db
from safechat.monitor import ChatMonitor

logger = logging.getLogger(__name__)

GROUPS = filters.ChatType.GROUPS
PRIVATE = filters.ChatType.PRIVATE

INTRO = (
    "Я SafeChatAI — наблюдаю за динамикой группового чата, распознаю назревающие конфликты с помощью ИИ "
    "и приватно предупреждаю медиатора, пока конфликт не вышел из-под контроля.\n\n"
    "Как начать:\n"
    "1. Добавьте меня в группу (в @BotFather у бота должен быть выключен privacy mode: /setprivacy → Disable).\n"
    "2. Администратор пишет в группе /setmediator — сам становится медиатором. "
    "Или отвечает этой командой на сообщение другого человека.\n"
    "3. Медиатор должен написать мне /start в личку — иначе я не смогу присылать уведомления.\n"
    "4. Сведения об участниках: ответьте в группе на сообщение человека командой /about <текст> "
    "или напишите мне в личку /about @username <текст>.\n\n"
    "Команды в личке для медиатора:\n"
    "/stats — статистика чатов и профилактика\n"
    "/advice — советы по последнему конфликту"
)


async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    member = await bot.get_chat_member(chat_id, user_id)
    return member.status in (ChatMember.OWNER, ChatMember.ADMINISTRATOR)


async def can_manage(bot: Bot, chat_id: int, user_id: int) -> bool:
    chat = await db.get_chat(chat_id)
    return (chat is not None and chat.mediator_id == user_id) or await is_admin(bot, chat_id, user_id)


async def try_dm(bot: Bot, user_id: int, text: str, **kwargs) -> bool:
    try:
        await bot.send_message(user_id, text[:4096], **kwargs)
        return True
    except Exception:
        return False


# ---------- Личка ----------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"Привет, {update.effective_user.full_name}!\n\n{INTRO}")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(INTRO)


async def cmd_about_private(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 2 or not context.args[0].startswith("@"):
        await update.message.reply_text("Формат: /about @username текст о человеке")
        return
    username, note = context.args[0], " ".join(context.args[1:])
    chats = await db.mediated_chats(update.effective_user.id)
    if not chats:
        await update.message.reply_text("Вы не медиатор ни одного чата.")
        return
    members = await db.find_member_by_username([c.id for c in chats], username)
    if not members:
        await update.message.reply_text(f"Не нашёл {username} в ваших чатах. Он должен хотя бы раз написать в группе.")
        return
    for m in members:
        await db.add_note(m.chat_id, m.user_id, m.name, m.username, note)
    await update.message.reply_text(f"Сохранил сведения о {members[0].name}.")


def format_stats(chat: db.Chat, members: list[db.Member], incidents: list[db.Incident]) -> str:
    names = {m.user_id: m.name for m in members}
    total = sum(m.message_count for m in members)
    lines = [f"📊 «{chat.title}»", f"Участников писало: {len(members)}, сообщений всего: {total}"]

    active = sorted(members, key=lambda m: m.message_count, reverse=True)[:5]
    if active:
        lines.append("Самые активные: " + ", ".join(f"{m.name} ({m.message_count})" for m in active))

    lines.append(f"\nКонфликтов за 30 дней: {len(incidents)}")
    if incidents:
        lines.append(f"Средний уровень: {sum(i.level for i in incidents) / len(incidents):.1f}/10")
        involved = Counter(uid for i in incidents for uid in i.participants)
        if involved:
            lines.append("Чаще всего вовлечены: " + ", ".join(
                f"{names.get(uid, uid)} ({n})" for uid, n in involved.most_common(5)))
        pairs = Counter(p for i in incidents for p in combinations(sorted(i.participants), 2))
        risky = [(p, n) for p, n in pairs.most_common(3) if n >= 2]
        if risky:
            lines.append("Повторяющиеся трения: " + ", ".join(
                f"{names.get(a, a)} ↔ {names.get(b, b)} ({n})" for (a, b), n in risky))
        lines.append(f"Последний: {incidents[-1].created_at:%d.%m %H:%M} UTC — {incidents[-1].summary}")
    return "\n".join(lines)


async def send_stats(bot: Bot, user_id: int, chats: list[db.Chat]) -> None:
    for chat in chats:
        text = format_stats(chat, await db.get_members(chat.id), await db.incidents_since(chat.id, 30))
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🛡 Советы по профилактике", callback_data=f"prevent:{chat.id}")
        ]])
        await bot.send_message(user_id, text[:4096], reply_markup=keyboard)


async def cmd_stats_private(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chats = await db.mediated_chats(update.effective_user.id)
    if not chats:
        await update.message.reply_text("Вы не медиатор ни одного чата.")
        return
    await send_stats(context.bot, update.effective_user.id, chats)


async def build_advice_context(incident: db.Incident) -> str:
    members = await db.get_members(incident.chat_id, set(incident.participants))
    return (
        f"Уровень конфликта: {incident.level:.1f}/10\n"
        f"Краткое описание: {incident.summary}\n\n"
        f"Участники:\n{ai.format_members(members)}\n\n"
        f"Переписка:\n{incident.dialog}"
    )


async def cmd_advice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chats = await db.mediated_chats(update.effective_user.id)
    incident = await db.last_incident([c.id for c in chats]) if chats else None
    if incident is None:
        await update.message.reply_text("Конфликтов в ваших чатах пока не было.")
        return
    await update.message.reply_text("Готовлю советы…")
    advice = await ai.mediation_advice(await build_advice_context(incident))
    await update.message.reply_text(f"💡 Советы по медиации\n\n{advice}"[:4096])


async def on_advice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    incident = await db.get_incident(int(query.data.split(":")[1]))
    chat = await db.get_chat(incident.chat_id) if incident else None
    if chat is None or chat.mediator_id != query.from_user.id:
        await query.answer("Недоступно", show_alert=True)
        return
    await query.answer("Готовлю советы…")
    advice = await ai.mediation_advice(await build_advice_context(incident))
    await query.message.reply_text(f"💡 Советы по медиации\n\n{advice}"[:4096])


async def on_prevent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat = await db.get_chat(int(query.data.split(":")[1]))
    if chat is None or chat.mediator_id != query.from_user.id:
        await query.answer("Недоступно", show_alert=True)
        return
    await query.answer("Анализирую статистику…")
    stats = format_stats(chat, await db.get_members(chat.id), await db.incidents_since(chat.id, 30))
    advice = await ai.prevention_advice(stats)
    await query.message.reply_text(f"🛡 Профилактика для «{chat.title}»\n\n{advice}"[:4096])


# ---------- Группы ----------

async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    change = update.my_chat_member
    was_in = change.old_chat_member.status in (ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER)
    is_in = change.new_chat_member.status in (ChatMember.MEMBER, ChatMember.ADMINISTRATOR)
    if is_in and not was_in and change.chat.type in ("group", "supergroup"):
        await context.bot.send_message(
            change.chat.id,
            "Привет! Я SafeChatAI — помогаю вовремя замечать конфликты.\n"
            "Администратор, назначьте медиатора командой /setmediator (себя) "
            "или ответьте ею на сообщение нужного человека. Подробнее — /help",
        )


async def cmd_setmediator(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, chat = update.message, update.effective_chat
    if not await is_admin(context.bot, chat.id, update.effective_user.id):
        await message.reply_text("Назначать медиатора могут только администраторы чата.")
        return
    target = message.reply_to_message.from_user if message.reply_to_message else update.effective_user
    await db.set_mediator(chat.id, chat.title or "", target.id)
    sent = await try_dm(context.bot, target.id, f"Вы назначены медиатором чата «{chat.title}». "
                                                "Я буду присылать сюда предупреждения о назревающих конфликтах.")
    if sent:
        await message.reply_text(f"Медиатор назначен: {target.full_name}.")
    else:
        await message.reply_text(f"Медиатор назначен: {target.full_name}. Напишите мне /start в личку, "
                                 "иначе я не смогу присылать уведомления.")


async def cmd_about_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, chat, user = update.message, update.effective_chat, update.effective_user
    if not await can_manage(context.bot, chat.id, user.id):
        return
    if not message.reply_to_message or not context.args:
        await try_dm(context.bot, user.id, "Ответьте на сообщение участника командой /about <текст о нём>.")
        return
    target = message.reply_to_message.from_user
    await db.add_note(chat.id, target.id, target.full_name, target.username, " ".join(context.args))
    try:
        await message.delete()  # сведения об участниках не должны висеть в общем чате
    except Exception:
        pass
    await try_dm(context.bot, user.id, f"Сохранил сведения о {target.full_name} (чат «{chat.title}»).")


async def cmd_stats_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id, user_id = update.effective_chat.id, update.effective_user.id
    if not await can_manage(context.bot, chat_id, user_id):
        return
    chat = await db.get_chat(chat_id)
    if chat is None:
        await update.message.reply_text("Пока нет данных.")
        return
    try:
        await send_stats(context.bot, user_id, [chat])
    except Exception:
        await update.message.reply_text("Напишите мне /start в личку — статистику я присылаю туда.")


async def on_group_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.message, update.effective_user
    if user is None or user.is_bot:
        return
    reply = message.reply_to_message
    await db.save_message(
        chat_id=message.chat.id,
        chat_title=message.chat.title or "",
        user_id=user.id,
        name=user.full_name,
        username=user.username,
        reply_to_name=reply.from_user.full_name if reply and reply.from_user else None,
        text=message.text,
    )
    monitor: ChatMonitor = context.bot_data["monitor"]
    monitor.on_message(message.chat.id)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Ошибка при обработке обновления", exc_info=context.error)


def register(app: Application) -> None:
    app.add_handler(CommandHandler("start", cmd_start, filters=PRIVATE))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("about", cmd_about_private, filters=PRIVATE))
    app.add_handler(CommandHandler("about", cmd_about_group, filters=GROUPS))
    app.add_handler(CommandHandler("stats", cmd_stats_private, filters=PRIVATE))
    app.add_handler(CommandHandler("stats", cmd_stats_group, filters=GROUPS))
    app.add_handler(CommandHandler("advice", cmd_advice, filters=PRIVATE))
    app.add_handler(CommandHandler("setmediator", cmd_setmediator, filters=GROUPS))
    app.add_handler(CallbackQueryHandler(on_advice, pattern=r"^advice:\d+$"))
    app.add_handler(CallbackQueryHandler(on_prevent, pattern=r"^prevent:-?\d+$"))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(GROUPS & filters.TEXT & ~filters.COMMAND, on_group_text))
    app.add_error_handler(on_error)
