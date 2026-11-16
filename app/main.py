"""Точка входа Life OS: FastAPI + Telegram-бот + APScheduler в одном процессе.

Запуск:  python -m app.main   (схема БД создаётся Alembic: alembic upgrade head)
"""
import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from app.bot.instance import bot, dp
from app.finance.router import router as finance_router
from app.finance.services.scheduler import setup_scheduler
from app.health.router import router as health_router
from app.health.dashboard import router as health_dashboard_router
from app.health.abs_forecast import router as abs_forecast_router
from app.users.router import router as users_router
from app.web import auth, pages
from app.web.report_render import router as report_render_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Жизненный цикл FastAPI: запуск и остановка планировщика APScheduler."""
    scheduler = setup_scheduler(bot)
    yield
    scheduler.shutdown(wait=False)
    logger.info("Планировщик APScheduler остановлен.")


app = FastAPI(
    title="Life OS — семейные финансы и здоровье",
    version="2.0.0",
    lifespan=lifespan,
)

# Авторизация и пользователи
app.include_router(auth.router)
app.mount('/static',StaticFiles(directory=str(Path(__file__).parent/'web'/'static')),name='static')
app.include_router(report_render_router)
app.include_router(users_router)
# Домены
app.include_router(finance_router)
app.include_router(health_router)
app.include_router(health_dashboard_router)
app.include_router(abs_forecast_router)
# HTML-страницы (Jinja2) и API дашборда
app.include_router(pages.router)


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
    logger.info("Старт единого сервиса (FastAPI + Telegram Bot + APScheduler)...")
    await asyncio.gather(
        start_fastapi(),
        start_bot(),
    )


if __name__ == "__main__":
    asyncio.run(main())
