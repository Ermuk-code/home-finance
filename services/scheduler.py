import logging
from datetime import datetime, timezone

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from db.base import async_session_maker
from db.models import Reminder

logger = logging.getLogger(__name__)


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
                rem.is_notified = True
                continue

            msg_text = (
                f"🔔 <b>Календарное напоминание!</b>\n\n"
                f"📌 <b>{rem.title}</b>\n"
            )
            if rem.description:
                msg_text += f"📝 {rem.description}\n"
            msg_text += f"\n⏰ План: <code>{rem.trigger_date.strftime('%d.%m.%Y %H:%M')}</code>"

            try:
                await bot.send_message(
                    chat_id=rem.user.telegram_id,
                    text=msg_text,
                    parse_mode="HTML",
                )
                rem.is_notified = True
                logger.info(
                    "Отправлено напоминание id=%s пользователю tg_id=%s",
                    rem.id,
                    rem.user.telegram_id,
                )
            except Exception as e:
                # Если пользователь заблокировал бота или не нажал /start, логируем без падения планировщика
                logger.warning(
                    "Не удалось отправить напоминание id=%s (tg_id=%s): %s",
                    rem.id,
                    rem.user.telegram_id,
                    e,
                )

        await session.commit()


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
    scheduler.start()
    logger.info("Фоновый планировщик APScheduler запущен (интервал: 1 мин).")
    return scheduler