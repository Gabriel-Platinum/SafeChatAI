"""SafeChatAI — бот-наблюдатель: распознаёт назревающие конфликты в групповом чате
и приватно уведомляет медиатора.

Локально работает через polling. На Render (есть RENDER_EXTERNAL_URL) — через webhook,
т.к. бесплатный тариф Render даёт только веб-сервисы.
"""

import asyncio
import logging

from telegram import BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats, Update
from telegram.ext import Application

from safechat import ai, config, db, handlers, texts
from safechat.monitor import ChatMonitor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # не логировать каждый запрос (в URL есть токен)
logger = logging.getLogger("safechat")


async def setup_bot_profile(app: Application) -> None:
    """Описание бота и меню команд: только в личке, в группах команд не показываем."""
    bot = app.bot
    commands = [
        BotCommand("start", "Главное меню"),
        BotCommand("chats", "Мои чаты"),
        BotCommand("stats", "Статистика"),
        BotCommand("help", "Как это работает"),
    ]
    try:
        await bot.delete_my_commands()
        await bot.delete_my_commands(scope=BotCommandScopeAllGroupChats())
        await bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
        await bot.set_my_description(texts.BOT_DESCRIPTION)
        await bot.set_my_short_description(texts.BOT_SHORT_DESCRIPTION)
    except Exception:
        logger.warning("Не удалось обновить описание и команды бота", exc_info=True)


async def on_startup(app: Application) -> None:
    await db.init_db()
    monitor = ChatMonitor(app.bot)
    app.bot_data["monitor"] = monitor
    await setup_bot_profile(app)
    await monitor.catch_up()
    app.bot_data["diag_task"] = asyncio.create_task(check_ai_connectivity())


async def check_ai_connectivity() -> None:
    """Проверяет связь с ИИ и записывает отчёт в базу (таблица diagnostics) — его видно без логов Render."""
    try:
        report = await ai.connectivity_report()
        logger.info("Проверка связи с ИИ:\n%s", report)
        await db.set_diagnostic("groq", report)
    except Exception:
        logger.exception("Проверка связи с ИИ не удалась")


def create_app() -> Application:
    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .concurrent_updates(True)  # долгие запросы к ИИ не блокируют остальные сообщения
        .post_init(on_startup)
        .build()
    )
    handlers.register(app)
    return app


if __name__ == "__main__":
    app = create_app()
    if config.WEBHOOK_BASE_URL:
        logger.info("Запуск в режиме webhook: %s", config.WEBHOOK_BASE_URL)
        app.run_webhook(
            listen="0.0.0.0",
            port=config.PORT,
            url_path=config.WEBHOOK_PATH,
            webhook_url=config.WEBHOOK_BASE_URL.rstrip("/") + "/" + config.WEBHOOK_PATH,
            secret_token=config.WEBHOOK_SECRET,
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        logger.info("Запуск в режиме polling")
        app.run_polling(allowed_updates=Update.ALL_TYPES)
