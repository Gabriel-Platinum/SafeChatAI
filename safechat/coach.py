"""«Помоги с конфликтом»: любой человек рассказывает о своём конфликте в переписке,
бот разбирает его по методу ненасильственного общения и предлагает, что написать.

Приватность: рассказ хранится только в памяти процесса на время разговора (SESSION_TTL)
и не пишется ни в базу, ни в логи.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field

from telegram import InlineKeyboardButton as Button
from telegram import InlineKeyboardMarkup, Message, Update
from telegram.ext import ContextTypes

from safechat import ai, config, texts

logger = logging.getLogger(__name__)

SESSION_TTL = 60 * 60  # час без активности — разговор забывается
MAX_STORY_CHARS = 6000
MAX_TURNS = 12  # сколько последних реплик разговора держим для ИИ
AI_SLOTS = asyncio.Semaphore(3)  # одновременные запросы к ИИ — чтобы наплыв на форуме не выбивал лимиты

GOALS = {
    "peace": "🤝 Хочу помириться",
    "heard": "🗣 Хочу, чтобы меня услышали",
    "understand": "🔍 Хочу понять, что произошло",
}
REFINE = {
    "softer": "Сделай предложенное сообщение мягче и теплее.",
    "shorter": "Сократи предложенное сообщение до 2-3 предложений.",
    "another": "Предложи другой вариант сообщения — с другим подходом.",
}


@dataclass
class Session:
    story: list[str] = field(default_factory=list)
    goal: str | None = None
    history: list[dict] = field(default_factory=list)  # реплики разговора с ИИ после разбора
    acknowledged: bool = False  # уже ответили «принял» на первую часть рассказа
    support_reason: str | None = None  # в рассказе тревожные признаки — только бережная поддержка
    updated: float = field(default_factory=time.monotonic)

    @property
    def analysed(self) -> bool:
        return bool(self.history)


_sessions: dict[int, Session] = {}


def get_session(user_id: int) -> Session | None:
    now = time.monotonic()
    for uid in [uid for uid, s in _sessions.items() if now - s.updated > SESSION_TTL]:
        del _sessions[uid]
    session = _sessions.get(user_id)
    if session:
        session.updated = now
    return session


def has_session(user_id: int) -> bool:
    return get_session(user_id) is not None


# ---------- Клавиатуры ----------

def done_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[Button("✅ Это всё, разобрать", callback_data="c:done")]])


def goals_keyboard() -> InlineKeyboardMarkup:
    rows = [[Button(label, callback_data=f"c:goal:{key}")] for key, label in GOALS.items()]
    return InlineKeyboardMarkup(rows)


def answer_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [Button("✏️ Мягче", callback_data="c:softer"), Button("📏 Короче", callback_data="c:shorter")],
        [Button("🔄 Другой вариант", callback_data="c:another")],
        [Button("🆕 Другой конфликт", callback_data="c:start")],
    ])


# ---------- Вход ----------

async def start(message: Message, user_id: int) -> None:
    _sessions[user_id] = Session()
    await message.reply_text(texts.COACH_INTRO)


async def cmd_conflict(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update.message, update.effective_user.id)


# ---------- Рассказ ----------

def describe(message: Message) -> str | None:
    """Текст сообщения; у пересланных — с именем автора, чтобы ИИ понимал, кто что писал."""
    text = message.text or message.caption
    if not text:
        return None
    origin = message.forward_origin
    if origin is None:
        return text
    author = (getattr(origin, "sender_user", None) and origin.sender_user.full_name) \
        or getattr(origin, "sender_user_name", None) or getattr(origin, "author_signature", None) or "Собеседник"
    if getattr(origin, "sender_user", None) and origin.sender_user.id == message.from_user.id:
        author = "Я"
    return f"[{author}]: {text}"


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Обрабатывает текст в личке, если идёт разговор о конфликте. Возвращает False, если разговора нет."""
    user_id = update.effective_user.id
    session = get_session(user_id)
    if session is None:
        return False

    text = describe(update.message)
    if text is None:
        await update.message.reply_text(texts.COACH_TEXT_ONLY)
        return True

    if session.analysed:  # вопрос после разбора
        await reply_with_ai(update.message, session, text)
        return True

    if sum(map(len, session.story)) + len(text) > MAX_STORY_CHARS:
        await update.message.reply_text(texts.COACH_TOO_LONG, reply_markup=done_keyboard())
        return True
    session.story.append(text)
    if not session.acknowledged:  # на пачку пересланных сообщений отвечаем один раз
        session.acknowledged = True
        await update.message.reply_text(texts.COACH_ACK, reply_markup=done_keyboard())
    return True


# ---------- Кнопки ----------

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from safechat.private import answer  # здесь, чтобы не было циклического импорта

    query = update.callback_query
    user_id = query.from_user.id
    action = query.data.split(":")[1]
    await answer(query)

    if action == "start":
        await start(query.message, user_id)
        return

    session = get_session(user_id)
    if session is None:
        await query.message.reply_text(texts.COACH_EXPIRED, reply_markup=InlineKeyboardMarkup(
            [[Button("🤝 Рассказать о конфликте", callback_data="c:start")]]))
        return

    if action == "done":
        if not session.story:
            await query.message.reply_text(texts.COACH_EMPTY)
            return
        await query.edit_message_reply_markup(None)
        await query.message.reply_text(texts.COACH_GOAL, reply_markup=goals_keyboard())
    elif action == "goal":
        session.goal = GOALS.get(query.data.split(":")[2], "")
        await query.edit_message_reply_markup(None)
        await reply_with_ai(query.message, session, None)
    elif action in REFINE and session.analysed:
        await query.edit_message_reply_markup(None)
        await reply_with_ai(query.message, session, REFINE[action])


# ---------- ИИ ----------

def build_messages(session: Session) -> list[dict]:
    story = "\n".join(session.story)
    goal = session.goal.split(" ", 1)[-1] if session.goal else "не указана"
    first = f"Мой рассказ о конфликте:\n{story}\n\nМоя цель: {goal}"
    system = [{"role": "system", "content": ai.COACH_PROMPT}]
    if session.support_reason:
        system.append({"role": "system", "content": ai.SUPPORT_NOTE.format(reason=session.support_reason)})
    return [*system, {"role": "user", "content": first}, *session.history[-MAX_TURNS:]]


async def check_safety(session: Session) -> None:
    """Перед разбором отдельная модель проверяет рассказ на тревожные признаки (в т.ч. косвенные,
    без «ключевых слов»). Если проверка недоступна — остаётся правило в инструкции помощника."""
    try:
        async with AI_SLOTS:
            real, kind, reason = await ai.safety_check(
                "\n".join(session.story), "", "Личный разговор человека с ботом-помощником о его конфликте.")
        if real and kind in ("self_harm", "threat"):
            session.support_reason = reason or kind
    except Exception as err:
        logger.warning("Помощник: проверка безопасности недоступна: %s", ai.error_chain(err, limit=2))


async def ask(messages: list[dict]) -> str:
    """Сильная модель, а при лимите или сбое — быстрая."""
    models = list(dict.fromkeys([config.ADVICE_MODEL, config.SCREEN_MODEL]))
    last_error = None
    async with AI_SLOTS:
        for model in models:
            try:
                response = await ai.client.chat.completions.create(
                    model=model, temperature=0.5, messages=messages, **ai._model_options(model, 1500))
                text = (response.choices[0].message.content or "").replace("**", "").strip()
                if text:
                    return text
            except Exception as err:
                last_error = err
                logger.warning("Помощник: модель %s недоступна: %s", model, ai.error_chain(err, limit=2))
    raise RuntimeError("ИИ недоступен") from last_error


async def reply_with_ai(message: Message, session: Session, user_turn: str | None) -> None:
    """Разбор (user_turn=None) или ответ на продолжение разговора."""
    placeholder = await message.reply_text(texts.COACH_THINKING)
    if not session.analysed:
        await check_safety(session)
    messages = build_messages(session)
    if user_turn:
        messages.append({"role": "user", "content": user_turn})
    try:
        reply = await ask(messages)
    except Exception:
        await placeholder.edit_text(texts.COACH_BUSY, reply_markup=InlineKeyboardMarkup(
            [[Button("🔁 Попробовать ещё раз", callback_data="c:goal:" + _goal_key(session))]])
            if not session.analysed else None)
        return

    if session.analysed:
        session.history.append({"role": "user", "content": user_turn})
    session.history.append({"role": "assistant", "content": reply})
    keyboard = None if session.support_reason else answer_keyboard()  # в поддержке нечего «смягчать»
    await placeholder.edit_text(reply[:4096], reply_markup=keyboard)


def _goal_key(session: Session) -> str:
    return next((key for key, label in GOALS.items() if label == session.goal), "peace")
