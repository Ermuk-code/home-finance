"""Финансовый домен: собирает все REST-роутеры финансов в один (подключается в app.main)."""
from fastapi import APIRouter

from app.finance.api import income, limits, reminders, transactions, uploads, vacations

router = APIRouter()
for _r in (transactions.router, uploads.router, reminders.router, vacations.router, limits.router, income.router):
    router.include_router(_r)
