import asyncio
import logging
from contextlib import asynccontextmanager
import uvicorn
from aiogram import Bot, Dispatcher
from fastapi import FastAPI

from config import settings
from db.base import async_session_maker
from bot.middlewares import DbSessionAndAuthMiddleware
from bot.handlers import base, documents, learning, invest
from services.scheduler import setup_scheduler
from web import auth, pages
from web.api import reminders, transactions, uploads, users
from db.base import Base, engine, async_session_maker
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Инициализация Telegram-бота и диспетчера
bot = Bot(token=settings.BOT_TOKEN)
dp = Dispatcher()
dp.update.middleware(DbSessionAndAuthMiddleware(session_pool=async_session_maker))

dp.include_router(base.router)
dp.include_router(documents.router)
dp.include_router(learning.router)
dp.include_router(invest.router)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Жизненный цикл FastAPI: запуск и остановка планировщика APScheduler."""
    scheduler = setup_scheduler(bot)
    yield
    scheduler.shutdown(wait=False)
    logger.info("Планировщик APScheduler остановлен.")


app = FastAPI(
    title="Домашний сервис контроля денежных средств",
    version="1.0.0",
    lifespan=lifespan,
)

# Подключаем REST API роутеры
app.include_router(auth.router)
app.include_router(transactions.router)
app.include_router(uploads.router)
app.include_router(users.router)
app.include_router(reminders.router)

# Подключаем HTML-страницы (Jinja2)
app.include_router(pages.router)


@app.get("/health", tags=["System"])
async def health_check():
    return {"status": "ok"}
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Жизненный цикл FastAPI: автосоздание новых таблиц и запуск планировщика APScheduler."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    scheduler = setup_scheduler(bot)
    yield
    scheduler.shutdown(wait=False)
    logger.info("Планировщик APScheduler остановлен.")

async def start_fastapi() -> None:
    config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


async def start_bot() -> None:
    logger.info("Запуск поллинга Telegram-бота...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


async def main() -> None:
    logger.info("Старт единого сервиса (FastAPI + Telegram Bot + APScheduler)...")
    await asyncio.gather(
        start_fastapi(),
        start_bot(),
    )


if __name__ == "__main__":
    asyncio.run(main())