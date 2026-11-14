import logging
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import async_session_maker
from app.health.fatsecret_client import is_configured as fatsecret_configured
from app.health.nutrition_sync import sync_all_users
from app.models import Category, Deposit, Reminder, Transaction, User
from app.finance.services.advisor import generate_smart_advice
from app.finance.services.budget import check_category_limits
from app.finance.services.recurrence import RecurrenceError, describe, next_occurrence
from app.finance.services.reports import format_report, summarize_transactions

logger = logging.getLogger(__name__)

# Еженедельная сводка: воскресенье, 18:00 по Москве.
WEEKLY_DIGEST_TZ = "Europe/Moscow"
WEEKLY_DIGEST_HOUR = 18
WEEKLY_DIGEST_DAYS = 7


async def create_deposit_end_reminder(session: AsyncSession, deposit: Deposit) -> Optional[Reminder]:
    """Создаёт напоминание «Закрытие вклада» за день до окончания срока.

    Возвращает None, если у вклада нет ``end_date`` (накопительный счёт) или напоминание уже
    потеряло смысл (день до окончания прошёл — иначе бот тут же прислал бы запоздалый «привет из прошлого»).
    Коммит остаётся за вызывающим кодом.
    """
    if deposit.end_date is None:
        return None

    trigger_date = deposit.end_date - timedelta(days=1)
    if trigger_date <= datetime.now(timezone.utc):
        return None

    reminder = Reminder(
        user_id=deposit.user_id,
        deposit_id=deposit.id,
        title=f"Закрытие вклада: {deposit.title}"[:150],
        description=f"{deposit.bank_name}: срок вклада заканчивается {deposit.end_date.strftime('%d.%m.%Y')}.",
        trigger_date=trigger_date,
        is_notified=False,
    )
    session.add(reminder)
    return reminder


def _finish_or_reschedule(rem: Reminder, now: datetime) -> None:
    """Разовое напоминание закрываем; повторяющееся переносим на ближайшую будущую дату."""
    if not rem.recurrence:
        rem.is_notified = True
        return
    try:
        rem.trigger_date = next_occurrence(
            rem.trigger_date, rem.recurrence, now, rem.recurrence_day, rem.recurrence_every or 1
        )
        rem.is_notified = False
    except RecurrenceError as e:
        # Битое правило не должно приводить к спаму каждую минуту — закрываем как разовое
        logger.warning("Напоминание id=%s: не удалось перенести повтор (%s), закрываю.", rem.id, e)
        rem.is_notified = True


def format_reminder_message(rem: Reminder) -> str:
    """HTML-текст напоминания для Telegram. Пользовательский текст экранируется: символы < и &
    в названии иначе ломают разметку, и Telegram отклоняет сообщение."""
    msg_text = (
        f"🔔 <b>Календарное напоминание!</b>\n\n"
        f"📌 <b>{escape(rem.title)}</b>\n"
    )
    if rem.description:
        msg_text += f"📝 {escape(rem.description)}\n"
    msg_text += f"\n⏰ План: <code>{rem.trigger_date.strftime('%d.%m.%Y %H:%M')}</code>"
    if rem.recurrence:
        msg_text += f"\n🔁 Повторяется: {escape(describe(rem.recurrence, rem.trigger_date, rem.recurrence_every or 1))}"
    return msg_text


async def check_reminders(bot: Bot) -> None:
    """
    Проверяет таблицу reminders на наличие наступивших событий
    и отправляет уведомления пользователям в Telegram.
    """
    now = datetime.now(timezone.utc)

    async with async_session_maker() as session:
        stmt = (
            select(Reminder)
            .options(selectinload(Reminder.user))
            .where(
                Reminder.is_notified.is_(False),
                Reminder.trigger_date <= now,
            )
        )
        due_reminders = (await session.execute(stmt)).scalars().all()

        if not due_reminders:
            return

        for rem in due_reminders:
            if not rem.user or not rem.user.telegram_id:
                _finish_or_reschedule(rem, now)
                continue

            msg_text = format_reminder_message(rem)

            try:
                await bot.send_message(
                    chat_id=rem.user.telegram_id,
                    text=msg_text,
                    parse_mode="HTML",
                )
                _finish_or_reschedule(rem, now)
                logger.info(
                    "Отправлено напоминание id=%s пользователю tg_id=%s",
                    rem.id,
                    rem.user.telegram_id,
                )
            except (TelegramBadRequest, TelegramForbiddenError) as e:
                # Постоянная ошибка (бот заблокирован, чат не найден, битая разметка): повтор через минуту
                # не поможет, а бесконечные попытки забивают лог. Закрываем/переносим как отправленное.
                logger.error(
                    "Напоминание id=%s (tg_id=%s) не доставлено, попыток больше не будет: %s",
                    rem.id,
                    rem.user.telegram_id,
                    e,
                )
                _finish_or_reschedule(rem, now)
            except Exception as e:  # noqa: BLE001
                # Временный сбой (сеть, таймаут, 429): оставляем как есть — попробуем на следующей минуте
                logger.warning(
                    "Не удалось отправить напоминание id=%s (tg_id=%s), повторю позже: %s",
                    rem.id,
                    rem.user.telegram_id,
                    e,
                )

        await session.commit()


async def build_weekly_digest(session: AsyncSession, user_id: int) -> Optional[str]:
    """Собирает текст еженедельной сводки: итоги за 7 дней, предупреждения по лимитам и советы.

    Возвращает None, если за неделю нет операций и нечего советовать — такому пользователю писать не надо.
    """
    since = datetime.now(timezone.utc) - timedelta(days=WEEKLY_DIGEST_DAYS)
    rows = (
        await session.execute(
            select(Category.name, Transaction.amount, Category.type)
            .outerjoin(Category, Transaction.category_id == Category.id)
            .where(Transaction.user_id == user_id, Transaction.date >= since)
        )
    ).all()

    warnings = await check_category_limits(session, user_id)
    advice = await generate_smart_advice(session, user_id)

    if not rows and not warnings and not advice:
        return None

    parts = ["🗓 <b>Еженедельная финансовая сводка</b>\n"]
    if rows:
        parts.append(format_report(summarize_transactions(rows), days=WEEKLY_DIGEST_DAYS))
    else:
        parts.append(f"📭 За последние {WEEKLY_DIGEST_DAYS} дней операций не было.")

    if warnings:
        parts.append("\n<b>Бюджетные лимиты:</b>\n" + "\n".join(warnings))
    if advice:
        parts.append("\n💡 <b>Советы:</b>\n" + "\n\n".join(advice))
    else:
        parts.append("\n💡 Существенных отклонений от ваших обычных трат за 3 месяца не найдено.")
    return "\n".join(parts)


async def send_weekly_digest(bot: Bot) -> None:
    """Воскресная рассылка: сводка + советы каждому пользователю в Telegram."""
    async with async_session_maker() as session:
        users = (await session.execute(select(User))).scalars().all()

        for user in users:
            if not user.telegram_id:
                continue
            try:
                text = await build_weekly_digest(session, user.id)
                if text is None:
                    continue
                await bot.send_message(chat_id=user.telegram_id, text=text, parse_mode="HTML")
                logger.info("Отправлена недельная сводка пользователю tg_id=%s", user.telegram_id)
            except Exception as e:
                # Один заблокировавший бота пользователь не должен ронять рассылку остальным
                logger.warning("Не удалось отправить недельную сводку (tg_id=%s): %s", user.telegram_id, e)


def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    """Инициализирует и запускает AsyncIOScheduler с интервалом проверки 1 минута."""
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        check_reminders,
        trigger="interval",
        minutes=1,
        kwargs={"bot": bot},
        id="check_reminders_job",
        replace_existing=True,
    )
    scheduler.add_job(
        send_weekly_digest,
        trigger=CronTrigger(day_of_week="sun", hour=WEEKLY_DIGEST_HOUR, minute=0, timezone=WEEKLY_DIGEST_TZ),
        kwargs={"bot": bot},
        id="weekly_digest_job",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    if fatsecret_configured():
        # Днём каждые 4 часа и итоги суток в 23:50 (с пушем «Итоги питания»)
        scheduler.add_job(
            sync_all_users,
            trigger=CronTrigger(hour="7,11,15,19", minute=0, timezone=settings.FATSECRET_TZ),
            id="nutrition_sync_job",
            replace_existing=True,
            misfire_grace_time=1800,
        )
        scheduler.add_job(
            sync_all_users,
            trigger=CronTrigger(hour=23, minute=50, timezone=settings.FATSECRET_TZ),
            kwargs={"notify_summary": True},
            id="nutrition_daily_summary_job",
            replace_existing=True,
            misfire_grace_time=1800,
        )
        logger.info("Синхронизация питания FatSecret: 07/11/15/19:00 и 23:50 (%s).", settings.FATSECRET_TZ)
    scheduler.start()
    logger.info("Фоновый планировщик APScheduler запущен (напоминания: 1 мин, недельная сводка: вс 18:00 МСК).")
    return scheduler