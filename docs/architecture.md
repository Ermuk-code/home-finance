# Архитектура

## Общая картина

```
 LAN-браузер ──HTTP:8000──┐
 Home Assistant ──webhook─┤
                          ▼
 ┌──────────────── LXC-контейнер (Proxmox) ────────────────┐
 │  docker compose                                          │
 │  ┌──────────── finance_app (python:3.12) ─────────────┐  │
 │  │  один процесс asyncio:                             │  │
 │  │   • FastAPI + uvicorn (веб-панель, REST API)       │  │
 │  │   • aiogram 3 (Telegram-бот, long polling)         │  │
 │  │   • APScheduler (напоминания, сводки, FatSecret)   │  │
 │  └───────────────┬────────────────────────────────────┘  │
 │                  │ asyncpg                               │
 │  ┌───────────────▼──────────┐                            │
 │  │ finance_db (postgres:16) │  том pgdata                │
 │  └──────────────────────────┘                            │
 └──────────────────────────────────────────────────────────┘
        │ исходящие HTTPS: api.telegram.org, iss.moex.com, FatSecret
```

Всё приложение — **один процесс** (`python -m app.main`): `asyncio.gather` запускает uvicorn и поллинг бота, а
планировщик стартует в `lifespan` FastAPI. Плюс: просто. Минусы: нельзя запустить несколько воркеров uvicorn
(задачи планировщика и бот задублируются), а падение одной части роняет всё.

## Структура кода (актуальная — пакет `app/`)

```
app/
├── main.py                 точка входа, сборка FastAPI, запуск бота и uvicorn
├── models.py               реестр всех ORM-моделей (импортировать модели отсюда)
├── core/
│   ├── config.py           Settings (pydantic-settings, читает .env)
│   ├── database.py         async engine, async_session_maker, Base, get_async_session
│   └── security.py         JWT, get_current_user, is_admin, DataScope / get_scope
├── users/                  пользователи и роли: models, router (/api/users), seed
├── finance/
│   ├── models.py           Category, CategoryLimit, Transaction, Asset, Deposit, Reminder, Vacation, IncomeRule
│   ├── router.py           собирает роутеры finance/api/*
│   ├── api/                transactions, uploads, reminders, vacations, limits, income
│   └── services/           бизнес-логика (см. ниже)
├── health/
│   ├── models.py           WeightLog, DailyNutrition, ActivityLog
│   ├── router.py           /api/health/*, вебхук веса, callback FatSecret
│   ├── fatsecret_client.py OAuth 1.0a-клиент FatSecret
│   ├── oauth1.py           подпись OAuth 1.0a (HMAC-SHA1)
│   └── nutrition_sync.py   UPSERT КБЖУ в daily_nutrition
├── notifications/sender.py send_telegram_message — «безопасная» отправка пуша
├── bot/
│   ├── instance.py         Bot, Dispatcher, подключение роутеров
│   ├── middlewares.py      сессия БД + проверка белого списка
│   ├── keyboards.py        reply-клавиатура
│   └── handlers/           base, admin, health, nutrition, deposits, limits, manual, documents, learning, invest
└── web/
    ├── auth.py             вход по паролю и по ссылке из бота, выход
    ├── pages.py            HTML-страницы + API дашборда, вкладов, активов, PDF портфеля
    └── templates/          Jinja2 + Tailwind (CDN) + Chart.js, логика страниц — inline JS
migrations/                 Alembic
tests/                      pytest, только unit-тесты без БД
homeassistant/              YAML для отправки веса из HA
scripts/backup.sh           ежедневный pg_dump (сервис backup в docker-compose)
.github/workflows/ci.yml    ruff + pytest + сборка Docker-образа
```

## Финансовые сервисы (`app/finance/services/`)

| Модуль | Назначение |
|---|---|
| `parser.py` | Разбор банковских выписок (Сбер, Альфа, Т-Банк; PDF через pdfplumber, CSV через pandas) → `ParsedTransaction` |
| `categorizer.py` | Справочник из 15 категорий с ключевыми словами, автокатегоризация, дедупликация при сохранении, перехват отпусков → «Путешествия» |
| `broker_parser.py` | Разбор брокерских отчётов (CSV/XLSX/PDF), определение типа бумаги, синхронизация `assets` |
| `tbank_report.py` | Специфичный разбор брокерского отчёта Т-Банка (ISIN, название, свободные рубли) |
| `moex_data.py` | Запросы к MOEX ISS: цены, купоны, погашения, дивиденды |
| `analytics.py` | Анализ портфеля: стоимость, P&L, доли, прогноз пассивного дохода, промпт для ИИ |
| `portfolio_insights.py` | «Наблюдения» по портфелю и подсказки по ребалансировке (70/30) |
| `portfolio_pdf.py` | PDF-досье портфеля (reportlab, шрифт DejaVu) |
| `deposits.py` | Валидация и расчёт вкладов/накопительных счетов, капитализация, доход |
| `manual.py` | Ручной ввод операций и наличные (`bank_name = "Наличные"`), парсинг коротких фраз |
| `income_analysis.py` | Разбор доходов по источникам (зарплата, родители, переводы…) по правилам `income_rules` |
| `limits.py`, `budget.py` | Месячные лимиты по категориям и предупреждения о превышении |
| `advisor.py` | «Умные советы»: сравнение трат с обычными за 3 месяца |
| `reports.py` | Сводки доход/расход для бота и дашборда |
| `recurrence.py` | Повторяющиеся напоминания (daily/weekly/monthly/yearly, «каждые N») |
| `scheduler.py` | Регистрация фоновых задач APScheduler |
| `invest_api.py` | Вспомогательный доступ к рыночным данным для бота |

## Изоляция данных и роли

Все пользовательские таблицы имеют `user_id → users.id (ON DELETE CASCADE)`.

* HTTP: зависимость `get_scope` возвращает `DataScope`. Запрос строится как `scope.apply(select(Model), Model)`,
  новые записи создаются с `user_id = scope.owner_id`.
* `user` видит только себя; `admin` — себя, чужой профиль (`?target_user_id=N`) или всех (`?all_users=true`).
* Бот: middleware находит пользователя по `telegram_id`; незарегистрированным отвечает отказом.
* Справочник категорий (`categories`) общий для всех.

## Аутентификация веб-панели

1. **admin** — `POST /api/login` с `ADMIN_USERNAME` / `ADMIN_PASSWORD` из `.env` (пароль может быть bcrypt-хешем).
2. **любой пользователь** — `/web` в боте → ссылка `/api/login/telegram?token=…` (JWT типа `login`, 5 минут).
3. Результат — HTTP-only cookie `access_token` (JWT типа `access`, 7 дней). Принимается и заголовок
   `Authorization: Bearer …`.

## Фоновые задачи (APScheduler, в процессе приложения)

| Задача | Расписание | Что делает |
|---|---|---|
| `check_reminders` | каждую минуту | Шлёт наступившие напоминания в Telegram; разовые закрывает, повторяющиеся переносит |
| `send_weekly_digest` | вс 18:00 МСК | Недельная сводка: итоги, лимиты, советы — каждому пользователю |
| `sync_all_users` | 07/11/15/19:00 (`FATSECRET_TZ`) | Подтягивает КБЖУ за сегодня из FatSecret |
| `sync_all_users(notify_summary)` | 23:50 | То же + пуш «Итоги питания за день» |

Задачи FatSecret регистрируются только если заданы `FATSECRET_CONSUMER_KEY/SECRET`.

## Основные потоки данных

* **Выписка банка**: файл в бот (`documents.py`) или на `/upload` → `BankStatementParser` → `save_parsed_transactions`
  (категоризация + отсев дублей) → `transactions`. Если файл не похож на выписку — пробуется `BrokerReportParser`
  → `assets` + `users.broker_cash_rub`.
* **Вес**: весы → Home Assistant → `POST /api/health/webhook/weight` → `weight_logs` → пуш владельцу замера.
* **Питание**: пользователь ведёт дневник в FatSecret → планировщик/`/nutrition` → `daily_nutrition` (UPSERT по дню).
* **Портфель**: `assets` + MOEX ISS → `analyze_portfolio` → дашборд, `/portfolio`, PDF.
