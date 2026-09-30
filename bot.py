"""SafeChatAI — бот-наблюдатель: распознаёт назревающие конфликты в групповом чате
и приватно уведомляет медиатора.

Локально работает через polling. На Render (есть RENDER_EXTERNAL_URL) — через webhook,
т.к. бесплатный тариф Render даёт только веб-сервисы.
"""

import logging

from telegram import Update
from telegram.ext import Application

from safechat import config, db, handlers
from safechat.monitor import ChatMonitor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # не логировать каждый запрос (в URL есть токен)
logger = logging.getLogger("safechat")


async def on_startup(app: Application) -> None:
    await db.init_db()
    app.bot_data["monitor"] = ChatMonitor(app.bot)


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
