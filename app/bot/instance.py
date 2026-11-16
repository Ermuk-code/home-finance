"""Экземпляры бота и диспетчера (общие для main и для пушей из FastAPI, например вебхука веса)."""
from aiogram import Bot, Dispatcher

from app.bot.handlers import admin, base, deposits, documents, invest, learning
from app.bot.handlers import health as health_handlers
from app.bot.handlers import limits as limits_handlers
from app.bot.handlers import manual as manual_handlers
from app.bot.handlers import nutrition as nutrition_handlers
from app.bot.handlers import workouts as workout_handlers
from app.bot.handlers import reports as report_handlers
from app.bot.middlewares import DbSessionAndAuthMiddleware
from app.core.config import settings
from app.core.database import async_session_maker

bot = Bot(token=settings.BOT_TOKEN)
dp = Dispatcher()
dp.update.middleware(DbSessionAndAuthMiddleware(session_pool=async_session_maker))

dp.include_router(base.router)
dp.include_router(report_handlers.router)
dp.include_router(admin.router)
dp.include_router(health_handlers.router)
dp.include_router(workout_handlers.router)
dp.include_router(nutrition_handlers.router)
dp.include_router(deposits.router)
dp.include_router(limits_handlers.router)
dp.include_router(manual_handlers.router)
dp.include_router(documents.router)
dp.include_router(learning.router)
dp.include_router(invest.router)
