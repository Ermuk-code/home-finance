#!/bin/sh
# Старт контейнера: миграции -> сид пользователей -> приложение
set -e

alembic upgrade head
python -m app.users.seed || echo "⚠️  Сид пользователей пропущен: проверьте ADMIN_TELEGRAM_ID / PARTNER_TELEGRAM_ID в .env"
exec python -m app.main
