"""Наблюдение за чатами: копит сообщения, запускает анализ и уведомляет медиатора."""

import asyncio
import logging
from collections import Counter
from datetime import timedelta
from html import escape as e

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.constants import ParseMode

from safechat import ai, config, db, texts

logger = logging.getLogger(__name__)


def short_model(model: str) -> str:
    return model.split("/")[-1]


def agreed_participants(verdicts: list[ai.Verdict]) -> list[str]:
    """Участники, которых назвало большинство моделей (иначе — по самой тревожной оценке)."""
    votes = Counter(n for v in verdicts for n in set(v.participants))
    agreed = sorted(n for n, c in votes.items() if c * 2 > len(verdicts))
    return agreed or sorted(max(verdicts, key=lambda v: v.level).participants)


def message_link(chat_id: int, tg_message_id: int | None) -> str | None:
    """Ссылка на сообщение. Работает только в супергруппах (ID вида -100…)."""
    raw = str(chat_id)
    if tg_message_id is None or not raw.startswith("-100"):
        return None
    return f"https://t.me/c/{raw[4:]}/{tg_message_id}"


def find_quote_source(quote: str, messages: list[db.ChatMessage]) -> db.ChatMessage | None:
    needle = quote.strip(" «»\"'.…").lower()[:40]
    if len(needle) < 5:
        return None
    for m in reversed(messages):
        if needle in m.text.lower():
            return m
    return None


class ChatMonitor:
    def __init__(self, bot: Bot):
        self.bot = bot
        self._timers: dict[int, asyncio.Task] = {}
        self._pending: dict[int, int] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()  # держим ссылки, чтобы фоновые задачи не собрал сборщик мусора

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def on_message(self, chat_id: int) -> None:
        """Откладывает анализ до паузы в переписке (или до BATCH_SIZE новых сообщений)."""
        self._pending[chat_id] = self._pending.get(chat_id, 0) + 1
        delay = 0 if self._pending[chat_id] >= config.BATCH_SIZE else config.DEBOUNCE_SECONDS
        self.schedule(chat_id, delay)

    def schedule(self, chat_id: int, delay: float) -> None:
        timer = self._timers.pop(chat_id, None)
        if timer:
            timer.cancel()
        self._timers[chat_id] = self._spawn(self._run_later(chat_id, delay))

    async def catch_up(self) -> None:
        """После перезапуска (Render засыпает) досматриваем сообщения, которые не успели проанализировать."""
        for chat_id in await db.chats_with_unanalyzed():
            logger.info("Чат %s: досматриваю сообщения после перезапуска", chat_id)
            self.schedule(chat_id, 5)

    async def _run_later(self, chat_id: int, delay: float) -> None:
        await asyncio.sleep(delay)
        self._timers.pop(chat_id, None)  # дальше анализ уже не отменяется новыми сообщениями
        lock = self._locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            self._pending[chat_id] = 0
            try:
                await self.analyze(chat_id)
            except Exception:
                logger.exception("Ошибка анализа чата %s", chat_id)

    async def analyze(self, chat_id: int) -> db.Incident | None:
        chat = await db.get_chat(chat_id)
        if chat is None or not chat.active:
            return None
        messages = await db.recent_messages(chat_id, config.JUDGE_WINDOW)
        if not messages or messages[-1].id <= chat.last_analyzed_id:
            return None
        if len(messages) < 3:
            return None

        screen_threshold, alert_threshold = config.SENSITIVITY.get(chat.sensitivity, config.SENSITIVITY["medium"])
        passport = ai.format_passport(chat)
        tension, reason = await ai.screen(ai.format_dialog(messages[-config.SCREEN_WINDOW:]), passport)
        await db.add_screening(chat_id, tension, messages[-1].id)
        logger.info("Чат %s: отсев %.0f/10 — %s", chat_id, tension, reason)
        if tension < screen_threshold:
            return None

        name_to_id = {m.name: m.user_id for m in messages}
        members = await db.get_members(chat_id, set(name_to_id.values()))
        previous = await db.last_incident([chat_id])
        dialog = ai.format_dialog(messages)
        context = f"О чате:\n{passport}\n\nСведения об участниках:\n{ai.format_members(members)}\n\n"
        if previous:
            context += f"Предыдущий инцидент в чате ({previous.created_at:%d.%m %H:%M} UTC): {previous.summary}\n\n"
        context += f"Переписка (время UTC):\n{dialog}"

        verdicts = await ai.judge(context)
        if not verdicts:
            return None
        level = sum(v.level for v in verdicts) / len(verdicts)
        logger.info("Чат %s: консилиум %s → %.1f", chat_id,
                    ", ".join(f"{short_model(v.model)}={v.level:.0f}" for v in verdicts), level)
        if level < alert_threshold:
            return None

        # Не дёргаем медиатора повторно, если это тот же конфликт и он не усилился.
        if previous and previous.created_at > db.utcnow() - timedelta(minutes=config.ALERT_COOLDOWN_MINUTES) \
                and level < previous.level + 2:
            logger.info("Чат %s: недавно уже уведомляли, пропускаем", chat_id)
            return None

        main = max(verdicts, key=lambda v: v.level)
        names = agreed_participants(verdicts)
        participant_ids = sorted({name_to_id[n] for n in names if n in name_to_id})
        incident = await db.create_incident(chat_id, level, participant_ids, main.summary, dialog)
        await self.notify(chat, incident, verdicts, names, messages)
        return incident

    async def notify(self, chat: db.Chat, incident: db.Incident, verdicts: list[ai.Verdict], names: list[str],
                     messages: list[db.ChatMessage]) -> None:
        if chat.mediator_id is None:
            logger.info("Чат %s: конфликт %.1f, но медиатора нет", chat.id, incident.level)
            return

        main = max(verdicts, key=lambda v: v.level)
        scores = ", ".join(f"{short_model(v.model)}: {v.level:.0f}" for v in verdicts)
        text = (
            f"⚠️ <b>Назревает конфликт</b> · «{e(chat.title)}»\n\n"
            f"Уровень: {incident.level:.1f}/10 ({scores})\n"
            f"Участники: {e(', '.join(names) or 'не определены')}\n\n"
            f"<b>Что происходит:</b> {e(main.summary)}\n"
        )
        if main.key_messages:
            lines = []
            for quote in main.key_messages:
                source = find_quote_source(quote, messages)
                link = message_link(chat.id, source.tg_message_id) if source else None
                lines.append(f'<a href="{link}">«{e(quote)}»</a>' if link else f"«{e(quote)}»")
            text += "\n<b>Ключевые сообщения:</b>\n" + "\n".join(lines) + "\n"
        if main.first_step:
            text += f"\n<b>Первый шаг:</b> {e(main.first_step)}"

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("💡 Советы по медиации", callback_data=f"advice:{incident.id}")
        ]])
        await self._send_to_mediator(chat, text, reply_markup=keyboard)

    # ---------- Тревожные сигналы ----------

    def on_safety(self, chat_id: int, kind: str, author: str, text: str) -> None:
        """Тревожный сигнал проверяем сразу, без ожидания паузы в переписке."""
        self._spawn(self._check_safety(chat_id, kind, author, text))

    async def _check_safety(self, chat_id: int, kind: str, author: str, text: str) -> None:
        try:
            chat = await db.get_chat(chat_id)
            if chat is None or not chat.active:
                return
            try:
                dialog = ai.format_dialog((await db.recent_messages(chat_id, 10))[:-1])
                real, ai_kind, reason = await ai.safety_check(f"{author}: {text}", dialog, ai.format_passport(chat))
                if ai_kind in texts.SAFETY_TITLE:
                    kind = ai_kind
            except Exception:
                logger.exception("Чат %s: ИИ не смог проверить тревожный сигнал — отправляю медиатору", chat_id)
                real, reason = True, "Я не смог проверить сообщение с помощью ИИ, поэтому присылаю на всякий случай."
            logger.info("Чат %s: тревожный сигнал %s, реальный=%s — %s", chat_id, kind, real, reason)
            if real:
                await self._send_to_mediator(chat, texts.safety_alert(kind, chat.title, author, text, reason))
        except Exception:
            logger.exception("Ошибка обработки тревожного сигнала в чате %s", chat_id)

    async def _send_to_mediator(self, chat: db.Chat, text: str, **kwargs) -> None:
        if chat.mediator_id is None:
            return
        try:
            await self.bot.send_message(chat.mediator_id, text[:4096], parse_mode=ParseMode.HTML,
                                        link_preview_options=LinkPreviewOptions(is_disabled=True), **kwargs)
        except Exception:
            logger.exception("Не удалось написать медиатору %s чата %s", chat.mediator_id, chat.id)
