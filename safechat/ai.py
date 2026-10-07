"""Работа с моделями Groq: отсев, консилиум моделей, советы медиатору."""

import asyncio
import json
import logging
from dataclasses import dataclass

from groq import AsyncGroq

from safechat import config
from safechat.db import Chat, ChatMessage, Member

logger = logging.getLogger(__name__)

client = AsyncGroq(api_key=config.GROQ_API_KEY, max_retries=1, timeout=40)

SCREEN_PROMPT = """Ты — система раннего обнаружения конфликтов в групповом чате. Тебе дают фрагмент переписки.
Оцени, нарастает ли в ней межличностное напряжение: взаимные упрёки и обвинения, сарказм в адрес участника, \
переход на личности, пассивная агрессия, резкая смена тона, игнорирование.
Учитывай контекст. НЕ является конфликтом: дружеские подколы, которые участники воспринимают нормально, \
эмоции по поводу ситуации (а не человека), мат без адресата, спокойный рабочий спор.
Ты только анализируешь, не отвечаешь участникам.
Верни строго JSON: {"tension": <целое 0-10>, "reason": "<одно предложение по-русски>"}"""

JUDGE_PROMPT = """Ты — опытный медиатор и аналитик групповой коммуникации. Задача — распознать назревающий \
или уже идущий межличностный конфликт в чате до того, как он выйдет из-под контроля.

Оценивай динамику, а не отдельные слова:
- кто к кому обращается и как меняется тон от сообщения к сообщению;
- взаимные обвинения, обобщения («ты всегда», «вечно ты»), сарказм, переход на личности, угрозы, игнорирование;
- повторяющиеся трения между одними и теми же людьми (см. сведения об участниках и прошлый инцидент).
НЕ считай конфликтом: дружеские подколы, эмоции по поводу ситуации, а не человека, рабочий спор в уважительном тоне.

Шкала conflict_level: 0-2 спокойно; 3-4 лёгкое напряжение; 5-6 назревает конфликт; \
7-8 открытый конфликт; 9-10 острая фаза (оскорбления, угрозы).

Верни строго JSON:
{"conflict_level": <целое 0-10>,
 "participants": ["<имена вовлечённых — точно как в переписке>"],
 "summary": "<2-3 предложения: суть разногласия и как оно развивается>",
 "key_messages": ["<до 3 коротких цитат, показывающих конфликт>"],
 "first_step": "<одна конкретная рекомендация медиатору, что сделать прямо сейчас>"}
Сведения об участниках от медиатора конфиденциальны: учитывай их, но не предлагай раскрывать или упоминать их участникам."""

ADVICE_PROMPT = """Ты — профессиональный медиатор. Помоги модератору группового чата мягко разрешить конфликт. \
Не занимай ничью сторону. Пиши по-русски, коротко и практично (до 1500 символов), простым текстом без markdown, \
списки — через «•».
Структура:
1. Что происходит (1-2 предложения).
2. Интересы и чувства каждой стороны.
3. Что написать прямо сейчас — готовая фраза для общего чата и, если нужно, для личных сообщений участникам.
4. Чего избегать.
5. Как не допустить повторения.
Сведения об участниках от медиатора конфиденциальны: учитывай их, но не предлагай раскрывать или упоминать их участникам."""

SAFETY_PROMPT = """Ты проверяешь сообщение из группового чата на тревожные сигналы: реальная угроза жизни или \
здоровью другого человека, либо признаки того, что автор думает о самоубийстве или самоповреждении.
Отличай реальные сигналы от гипербол и шуток между своими («убью тебя, если опоздаешь 😂», «я умер со смеху»). \
Явные маркеры шутки (😂, «ахах», условная гипербола вида «если …, я тебя убью») обычно означают шутку, даже если до этого в чате был спор: оценивай само сообщение и то, как автор относится к адресату. Учитывай нормы чата. При сомнении в признаках суицида или самоповреждения считай сигнал реальным — безопасность важнее.
Верни строго JSON: {"real": true или false, "kind": "self_harm" или "threat" или "none", \
"reason": "<одно предложение по-русски: почему>"}"""

PREVENTION_PROMPT = """Ты — медиатор и консультант по здоровой коммуникации, работаешь по методу ненасильственного \
общения (ННО: наблюдения, чувства, потребности, просьбы). По статистике группового чата дай медиатору 3-5 рекомендаций \
по профилактике конфликтов.

Правила:
- Только поддерживающие меры: личные разговоры, договорённости, внимание к потребностям людей, признание их вклада. \
Никаких наказаний, ограничений, лимитов сообщений, тайм-аутов, банов и публичных замечаний конкретным людям.
- Опирайся только на факты из статистики, ничего не выдумывай. Не навешивай ярлыков («конфликтный», «токсичный») \
и не делай выводов о людях сверх данных.
- Повторяющиеся трения между двумя людьми — подскажи, как помочь им договориться: например, разговор с каждым \
по отдельности, затем совместная встреча с медиатором по шагам ННО.
- Не поручай роль помощника или миротворца тем, кто сам часто участвует в конфликтах.
- Учитывай тип чата и его нормы общения.
- Каждая рекомендация конкретна и выполнима за неделю: что сделать, с кем и зачем.

По-русски, простым текстом без markdown, списком через «•»."""


@dataclass
class Verdict:
    model: str
    level: float
    participants: list[str]
    summary: str
    key_messages: list[str]
    first_step: str


def format_dialog(messages: list[ChatMessage]) -> str:
    lines = []
    for m in messages:
        who = m.name + (f" → {m.reply_to_name}" if m.reply_to_name else "")
        lines.append(f"[{m.created_at:%H:%M}] {who}: {m.text}")
    return "\n".join(lines)


def format_passport(chat: Chat) -> str:
    """Паспорт чата для ИИ: что за чат и какие в нём нормы общения."""
    from safechat.texts import CHAT_KINDS

    parts = []
    if chat.kind:
        parts.append("Тип чата: " + CHAT_KINDS.get(chat.kind, chat.kind).split(" ", 1)[-1])
    if chat.purpose:
        parts.append(f"Цель чата: {chat.purpose}")
    if chat.norms:
        parts.append(f"Принятые нормы общения (со слов медиатора): {chat.norms}")
    return "\n".join(parts) or "Сведений о чате нет."


def format_members(members: list[Member]) -> str:
    if not members:
        return "нет данных"
    lines = []
    for m in members:
        line = f"• {m.name}: сообщений {m.message_count}, участвовал(а) в конфликтах {m.incident_count} раз"
        if m.notes:
            line += f". Со слов медиатора: {m.notes.replace(chr(10), '; ')}"
        lines.append(line)
    return "\n".join(lines)


def _model_options(model: str, max_tokens: int) -> dict:
    # Ограничение длины ответа обязательно: на бесплатном Groq есть лимит выходных токенов в минуту,
    # и без max_tokens запрос могут отклонить заранее. Короткое «рассуждение» gpt-oss экономит
    # в 2-3 раза токены без потери качества оценки.
    options = {"max_tokens": max_tokens}
    if model.startswith("openai/gpt-oss"):
        options["reasoning_effort"] = "low"
    return options


async def _ask_json(model: str, system: str, user: str) -> dict:
    response = await client.chat.completions.create(
        model=model,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        **_model_options(model, 800),
    )
    return json.loads(response.choices[0].message.content)


async def _ask_text(model: str, system: str, user: str) -> str:
    response = await client.chat.completions.create(
        model=model,
        temperature=0.4,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        **_model_options(model, 1500),
    )
    # Telegram показываем простым текстом, поэтому убираем markdown-выделение.
    return response.choices[0].message.content.replace("**", "").strip()


def _clamp(value) -> float:
    return max(0.0, min(10.0, float(value)))


async def screen(dialog: str, passport: str) -> tuple[float, str]:
    """Быстрая оценка напряжения. Если модель упёрлась в лимит — пробуем следующую."""
    models = list(dict.fromkeys([config.SCREEN_MODEL, *config.JUDGE_MODELS]))
    user = f"О чате:\n{passport}\n\nПереписка:\n{dialog}"
    for i, model in enumerate(models):
        try:
            data = await _ask_json(model, SCREEN_PROMPT, user)
            return _clamp(data["tension"]), str(data.get("reason", ""))
        except Exception as e:
            if i == len(models) - 1:
                raise
            logger.warning("Отсев: модель %s недоступна (%s), пробую %s", model, e, models[i + 1])


async def judge(context: str) -> list[Verdict]:
    """Независимая оценка несколькими моделями. Возвращает ответы тех, что справились."""
    results = await asyncio.gather(
        *(_ask_json(model, JUDGE_PROMPT, context) for model in config.JUDGE_MODELS), return_exceptions=True
    )
    verdicts = []
    for model, data in zip(config.JUDGE_MODELS, results):
        if isinstance(data, Exception):
            logger.warning("Модель %s не ответила: %s", model, data)
            continue
        try:
            verdicts.append(Verdict(
                model=model,
                level=_clamp(data["conflict_level"]),
                participants=[str(p) for p in data.get("participants") or []],
                summary=str(data.get("summary", "")),
                key_messages=[str(q) for q in data.get("key_messages") or []][:3],
                first_step=str(data.get("first_step", "")),
            ))
        except (KeyError, TypeError, ValueError):
            logger.warning("Модель %s вернула некорректный ответ: %r", model, data)
    return verdicts


async def safety_check(message: str, dialog: str, passport: str) -> tuple[bool, str, str]:
    """(реальный ли сигнал, тип self_harm/threat/none, объяснение)."""
    user = f"О чате:\n{passport}\n\nПереписка перед сообщением:\n{dialog}\n\nПроверяемое сообщение:\n{message}"
    data = await _ask_json(config.ADVICE_MODEL, SAFETY_PROMPT, user)
    return bool(data.get("real")), str(data.get("kind", "none")), str(data.get("reason", ""))


async def mediation_advice(context: str) -> str:
    return await _ask_text(config.ADVICE_MODEL, ADVICE_PROMPT, context)


async def prevention_advice(stats: str) -> str:
    return await _ask_text(config.ADVICE_MODEL, PREVENTION_PROMPT, stats)
