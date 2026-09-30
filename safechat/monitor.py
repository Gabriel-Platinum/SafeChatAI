"""Наблюдение за чатами: копит сообщения, запускает анализ и уведомляет медиатора."""

import asyncio
import logging
from collections import Counter
from datetime import timedelta

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from safechat import ai, config, db

logger = logging.getLogger(__name__)


def short_model(model: str) -> str:
    return model.split("/")[-1]


def agreed_participants(verdicts: list[ai.Verdict]) -> list[str]:
    """Участники, которых назвало большинство моделей (иначе — по самой тревожной оценке)."""
    votes = Counter(n for v in verdicts for n in set(v.participants))
    agreed = sorted(n for n, c in votes.items() if c * 2 > len(verdicts))
    return agreed or sorted(max(verdicts, key=lambda v: v.level).participants)


class ChatMonitor:
    def __init__(self, bot: Bot):
        self.bot = bot
        self._timers: dict[int, asyncio.Task] = {}
        self._pending: dict[int, int] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def on_message(self, chat_id: int) -> None:
        """Откладывает анализ до паузы в переписке (или до BATCH_SIZE новых сообщений)."""
        self._pending[chat_id] = self._pending.get(chat_id, 0) + 1
        timer = self._timers.pop(chat_id, None)
        if timer:
            timer.cancel()
        delay = 0 if self._pending[chat_id] >= config.BATCH_SIZE else config.DEBOUNCE_SECONDS
        self._timers[chat_id] = asyncio.create_task(self._run_later(chat_id, delay))

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
        messages = await db.recent_messages(chat_id, config.JUDGE_WINDOW)
        if len(messages) < 3:
            return None

        tension, reason = await ai.screen(ai.format_dialog(messages[-config.SCREEN_WINDOW:]))
        logger.info("Чат %s: отсев %.0f/10 — %s", chat_id, tension, reason)
        if tension < config.SCREEN_THRESHOLD:
            return None

        name_to_id = {m.name: m.user_id for m in messages}
        members = await db.get_members(chat_id, set(name_to_id.values()))
        previous = await db.last_incident([chat_id])
        dialog = ai.format_dialog(messages)
        context = f"Сведения об участниках:\n{ai.format_members(members)}\n\n"
        if previous:
            context += f"Предыдущий инцидент в чате ({previous.created_at:%d.%m %H:%M} UTC): {previous.summary}\n\n"
        context += f"Переписка (время UTC):\n{dialog}"

        verdicts = await ai.judge(context)
        if not verdicts:
            return None
        level = sum(v.level for v in verdicts) / len(verdicts)
        logger.info("Чат %s: консилиум %s → %.1f", chat_id,
                    ", ".join(f"{short_model(v.model)}={v.level:.0f}" for v in verdicts), level)
        if level < config.ALERT_THRESHOLD:
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
        await self.notify(chat_id, incident, verdicts, names)
        return incident

    async def notify(self, chat_id: int, incident: db.Incident, verdicts: list[ai.Verdict], names: list[str]) -> None:
        chat = await db.get_chat(chat_id)
        if chat is None or chat.mediator_id is None:
            logger.info("Чат %s: конфликт %.1f, но медиатор не назначен", chat_id, incident.level)
            return

        main = max(verdicts, key=lambda v: v.level)
        names = names or ["не определены"]
        scores = ", ".join(f"{short_model(v.model)}: {v.level:.0f}" for v in verdicts)
        text = (
            f"⚠️ Назревает конфликт в чате «{chat.title}»\n\n"
            f"Уровень: {incident.level:.1f}/10 ({scores})\n"
            f"Участники: {', '.join(names)}\n\n"
            f"Что происходит: {main.summary}\n"
        )
        quotes = main.key_messages
        if quotes:
            text += "\nКлючевые сообщения:\n" + "\n".join(f"«{q}»" for q in quotes) + "\n"
        if main.first_step:
            text += f"\nПервый шаг: {main.first_step}"

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("💡 Советы по медиации", callback_data=f"advice:{incident.id}")
        ]])
        try:
            await self.bot.send_message(chat.mediator_id, text[:4096], reply_markup=keyboard)
        except Exception:
            logger.exception("Не удалось написать медиатору %s (он запускал бота в личке?)", chat.mediator_id)
