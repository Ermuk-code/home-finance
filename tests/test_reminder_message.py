"""Текст напоминания для Telegram: пользовательский ввод экранируется, иначе Telegram отклоняет HTML."""
from datetime import datetime, timezone

from app.finance.services.scheduler import format_reminder_message
from app.models import Reminder


def _reminder(**kw) -> Reminder:
    data = dict(
        title="Оплатить <ЖКХ> & интернет",
        description="Сумма < 5000 & > 3000",
        trigger_date=datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc),
        recurrence=None,
        recurrence_every=1,
    )
    data.update(kw)
    return Reminder(**data)


def test_user_text_is_escaped():
    text = format_reminder_message(_reminder())
    assert "&lt;ЖКХ&gt; &amp; интернет" in text
    assert "Сумма &lt; 5000 &amp; &gt; 3000" in text
    assert "<ЖКХ>" not in text


def test_own_markup_kept_and_no_description():
    text = format_reminder_message(_reminder(title="Встреча", description=None))
    assert "<b>Встреча</b>" in text
    assert "📝" not in text
    assert "<code>20.11.2026 09:00</code>" in text


def test_recurrence_line():
    text = format_reminder_message(_reminder(recurrence="monthly"))
    assert "🔁 Повторяется:" in text
