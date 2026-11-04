import asyncio
import logging
import uvicorn
from aiogram import Bot, Dispatcher
from fastapi import FastAPI

from config import settings
from db.base import async_session_maker
from bot.middlewares import DbSessionAndAuthMiddleware
from bot.handlers import base, documents, learning, invest
from web import auth
from web.api import transactions, uploads, users

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Домашний сервис контроля денежных средств",
    version="1.0.0",
)

# Подключаем REST API роутеры (День 5)
app.include_router(auth.router)
app.include_router(transactions.router)
app.include_router(uploads.router)
app.include_router(users.router)

# Инициализация Telegram-бота и диспетчера
bot = Bot(token=settings.BOT_TOKEN)
dp = Dispatcher()

# Подключаем проверку белого списка и сессию БД
dp.update.middleware(DbSessionAndAuthMiddleware(session_pool=async_session_maker))

# Регистрируем роутеры бота
dp.include_router(base.router)
dp.include_router(documents.router)
dp.include_router(learning.router)
dp.include_router(invest.router)


@app.get("/health", tags=["System"])
async def health_check():
    return {"status": "ok"}


async def start_fastapi() -> None:
    config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


async def start_bot() -> None:
    logger.info("Запуск поллинга Telegram-бота...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


async def main() -> None:
    logger.info("Старт единого сервиса (FastAPI + Telegram Bot)...")
    await asyncio.gather(
        start_fastapi(),
        start_bot(),
    )


if __name__ == "__main__":
    asyncio.run(main())