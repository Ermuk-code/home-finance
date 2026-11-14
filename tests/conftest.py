"""Тестовое окружение: подставляем обязательные переменные, чтобы `Settings()` создавался без .env."""
import os

for _key, _value in {
    "BOT_TOKEN": "123456:TEST",
    "POSTGRES_USER": "test",
    "POSTGRES_PASSWORD": "test",
    "POSTGRES_DB": "test",
    "POSTGRES_HOST": "localhost",
    "JWT_SECRET_KEY": "test-secret-key-for-unit-tests-only",
    "ADMIN_USERNAME": "admin",
    "ADMIN_PASSWORD": "admin",
}.items():
    os.environ.setdefault(_key, _value)
