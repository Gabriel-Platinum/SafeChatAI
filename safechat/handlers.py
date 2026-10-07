"""Регистрация обработчиков и временные инструменты медиатора (статистика, советы, заметки).

Статистика, советы и заметки об участниках будут переработаны в следующих фазах
(см. docs/business-process.md); пока они работают в упрощённом виде.
"""

import logging
from collections import Counter
from itertools import combinations

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, ChatMemberHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from safechat import ai, db, group, private

logger = logging.getLogger(__name__)

GROUPS = filters.ChatType.GROUPS
PRIVATE = filters.ChatType.PRIVATE
NEW = filters.UpdateType.MESSAGE  # без этого MessageHandler ловит и правки сообщений


# ---------- Заметки об участниках (до фазы 2) ----------

async def cmd_about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


# ---------- Статистика (до фазы 5) ----------

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


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chats = await db.mediated_chats(update.effective_user.id)
    if not chats:
        await update.message.reply_text("Вы не медиатор ни одного чата.")
        return
    await send_stats(context.bot, update.effective_user.id, chats)


async def on_stats_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    target = query.data.split(":")[1]
    if target == "all":
        chats = await db.mediated_chats(query.from_user.id)
    else:
        chat = await private.own_chat(query.from_user.id, int(target))
        chats = [chat] if chat else []
    if not chats:
        await query.answer("Нет доступных чатов", show_alert=True)
        return
    await query.answer()
    await send_stats(context.bot, query.from_user.id, chats)


async def on_prevent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat = await private.own_chat(query.from_user.id, int(query.data.split(":")[1]))
    if chat is None:
        await query.answer("Недоступно", show_alert=True)
        return
    await query.answer("Анализирую статистику…")
    stats = format_stats(chat, await db.get_members(chat.id), await db.incidents_since(chat.id, 30))
    advice = await ai.prevention_advice(stats)
    await query.message.reply_text(f"🛡 Профилактика для «{chat.title}»\n\n{advice}"[:4096])


# ---------- Советы по медиации (до фазы 3) ----------

async def build_advice_context(incident: db.Incident) -> str:
    chat = await db.get_chat(incident.chat_id)
    members = await db.get_members(incident.chat_id, set(incident.participants))
    return (
        f"О чате:\n{ai.format_passport(chat)}\n\n"
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
    chat = await private.own_chat(query.from_user.id, incident.chat_id) if incident else None
    if chat is None:
        await query.answer("Недоступно", show_alert=True)
        return
    await query.answer("Готовлю советы…")
    advice = await ai.mediation_advice(await build_advice_context(incident))
    await query.message.reply_text(f"💡 Советы по медиации\n\n{advice}"[:4096])


# ---------- Регистрация ----------

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Ошибка при обработке обновления", exc_info=context.error)


def register(app: Application) -> None:
    # Личка
    app.add_handler(CommandHandler("start", private.cmd_start, filters=PRIVATE & NEW))
    app.add_handler(CommandHandler("help", private.cmd_help, filters=PRIVATE & NEW))
    app.add_handler(CommandHandler("chats", private.cmd_chats, filters=PRIVATE & NEW))
    app.add_handler(CommandHandler("about", cmd_about, filters=PRIVATE & NEW))
    app.add_handler(CommandHandler("stats", cmd_stats, filters=PRIVATE & NEW))
    app.add_handler(CommandHandler("advice", cmd_advice, filters=PRIVATE & NEW))
    app.add_handler(CallbackQueryHandler(private.on_menu, pattern=r"^m:"))
    app.add_handler(CallbackQueryHandler(on_advice, pattern=r"^advice:\d+$"))
    app.add_handler(CallbackQueryHandler(on_prevent, pattern=r"^prevent:-?\d+$"))
    app.add_handler(CallbackQueryHandler(on_stats_button, pattern=r"^stats:(all|-?\d+)$"))
    app.add_handler(MessageHandler(PRIVATE & NEW & filters.TEXT & ~filters.COMMAND, private.on_private_text))

    # Группы
    app.add_handler(ChatMemberHandler(group.on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CommandHandler("start", group.on_group_start, filters=GROUPS & NEW))
    app.add_handler(MessageHandler(GROUPS & filters.StatusUpdate.MIGRATE, group.on_migrate))
    app.add_handler(MessageHandler(GROUPS & filters.StatusUpdate.NEW_CHAT_MEMBERS, group.on_new_members))
    app.add_handler(MessageHandler(GROUPS & filters.StatusUpdate.LEFT_CHAT_MEMBER, group.on_left_member))
    app.add_handler(MessageHandler(GROUPS & NEW & (filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
                                   group.on_group_message))
    app.add_handler(MessageHandler(GROUPS & filters.UpdateType.EDITED_MESSAGE, group.on_group_edit))

    app.add_error_handler(on_error)
