"""Базовый Telegram-бот на aiogram 3.

Токен берётся из переменной окружения BOT_TOKEN (файл .env).
"""

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN. Скопируйте .env.example в .env и впишите токен.")

logging.basicConfig(level=logging.INFO)

dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(f"Привет, {message.from_user.full_name}! Я SafeChatAI бот. Напиши /help.")


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer("/start — приветствие\n/help — список команд\nЛюбой текст — я его повторю.")


@dp.message(F.text)
async def echo(message: Message) -> None:
    await message.answer(message.text)


async def main() -> None:
    bot = Bot(token=BOT_TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
