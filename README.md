# Life OS — семейные финансы и здоровье

Единый сервис (FastAPI + Telegram-бот + APScheduler) для двух человек: финансы и вес/питание/активность.
Каждая запись привязана к пользователю (`user_id` → `users.id`), права разделены ролями `admin` и `user`.

📚 Подробная документация — в [docs/](docs/README.md): архитектура, модель данных, API, бот, развёртывание в LXC,
интеграции и [аудит с планом работ](docs/audit-2026-11.md).

## Структура

```
app/
├── main.py              # точка входа: python -m app.main
├── models.py            # реестр всех моделей (для Alembic и разрешения связей между доменами)
├── core/
│   ├── config.py        # настройки из .env
│   ├── database.py      # engine, сессия, Base
│   └── security.py      # JWT, get_current_user, is_admin, get_scope (изоляция данных)
├── users/               # пользователи и роли
│   ├── models.py        # users (id, telegram_id, name, role)
│   ├── router.py        # /api/users: профиль, управление (admin)
│   └── seed.py          # python -m app.users.seed — заводит admin и user из .env
├── finance/             # финансы (все таблицы с user_id)
│   ├── models.py, router.py
│   ├── api/             # transactions, uploads, reminders, vacations, limits, income
│   └── services/        # парсеры выписок, категоризатор, отчёты, MOEX, планировщик…
├── health/              # здоровье
│   ├── models.py        # weight_logs, daily_nutrition, activity_logs
│   ├── schemas.py       # WeightWebhookPayload и др.
│   ├── oauth1.py, fatsecret_client.py, nutrition_sync.py   # FatSecret: OAuth 1.0a, клиент, UPSERT в daily_nutrition
│   └── router.py        # /api/health/*, вебхук Home Assistant
├── notifications/       # sender.py — адресные push в Telegram
├── bot/                 # Telegram-бот (instance.py, middlewares, handlers/)
└── web/                 # вход, HTML-страницы (Jinja2) и API дашборда
migrations/              # Alembic
tests/
```

## Запуск и обновление существующей БД

1. Добавьте в `.env` новые переменные из `.env.example`:
   `ADMIN_NAME`, `PARTNER_TELEGRAM_ID`, `PARTNER_NAME`, `WEB_BASE_URL`, `HEALTH_WEBHOOK_TOKEN`.
2. `docker compose up -d --build` — контейнер сам выполнит `alembic upgrade head`, затем `python -m app.users.seed`
   и запустит приложение. Вручную:
   ```bash
   alembic upgrade head
   python -m app.users.seed
   python -m app.main
   ```
3. Миграция `7c1d2e9f4a60` не теряет данных: владелец (`ADMIN_TELEGRAM_ID`, иначе самый старый пользователь)
   становится `admin`, его правила доходов и месячные лимиты переходят к нему.

## Роли и изоляция данных

* `user` — все запросы автоматически получают `WHERE <таблица>.user_id = <свой id>`; чужие записи недоступны
  (чужой id в `target_user_id` → 403, обращение к чужой записи по id → 404).
* `admin` — по умолчанию видит свои данные; может добавить `?target_user_id=2` (профиль партнёра)
  или `?all_users=true` (без фильтра).
* Как это устроено: зависимость `get_scope` (`app/core/security.py`) возвращает `DataScope`; в запросах пишется
  `scope.apply(select(Model), Model)`, а новые записи создаются с `scope.owner_id`.
* Справочник категорий общий; **лимиты по категориям теперь персональные** (таблица `category_limits`),
  правила разбора доходов (`income_rules`) — тоже у каждого свои.

### Вход в веб-панель
* admin — логин/пароль из `.env` (`ADMIN_USERNAME` / `ADMIN_PASSWORD`);
* любой пользователь — команда `/web` в боте присылает ссылку (действует 5 минут) → cookie-сессия на 7 дней.

### Бот
Все сообщения проверяются по таблице `users`; незарегистрированные получают отказ.
Меню: «Отчёт», «Лимиты», «Вес», «Веб-панель»; у admin дополнительно «👥 Аналитика пользователей» (`/admin_stats`).

## Вес из Home Assistant

1. В `.env` задайте `HEALTH_WEBHOOK_TOKEN` и `APP_BIND_IP` (LAN-адрес хоста или `0.0.0.0`, иначе порт 8000 доступен только с localhost и Home Assistant до него не достучится). Опционально `WEIGHT_ADMIN_DIGEST=true` — admin получает «партнёр взвесился» без значения веса.
2. В Home Assistant добавьте `homeassistant/rest_command.yaml` (токен — в `secrets.yaml`) и вызов `rest_command.send_weight_to_life_os` в ветки `choose` автоматизации, см. `homeassistant/automation_snippet.yaml`.

Запрос:

```
POST /api/health/webhook/weight
X-Webhook-Token: <HEALTH_WEBHOOK_TOKEN>
{"profile_alias": "admin", "weight_kg": 82.4}
```

`profile_alias` — роль (`admin` / `user`), имя из `users.name` (без учёта регистра), числовой id или telegram_id
(старое имя поля `target_user` тоже принимается). Сервер сохраняет замер в `weight_logs` за найденным пользователем
и отправляет пуш только в его личный чат. Ответ `200 OK`; без `HEALTH_WEBHOOK_TOKEN` эндпоинт отвечает 503,
с неверным токеном 401, неизвестный профиль 404, неуникальный 409.

## Питание из FatSecret

FatSecret отдаёт дневник питания только через **OAuth 1.0a** (OAuth 2.0 у них — лишь для публичных данных), поэтому привязка идёт по схеме «request token → разрешить → access token». Токены бессрочные, refresh не нужен.

1. В [FatSecret Platform](https://platform.fatsecret.com) создайте приложение, возьмите Consumer Key и Shared Secret (раздел OAuth 1.0), впишите их в `.env` (`FATSECRET_CONSUMER_KEY`, `FATSECRET_CONSUMER_SECRET`). В настройках приложения добавьте IP сервера в белый список, если консоль его требует.
2. `WEB_BASE_URL` должен открываться в браузере пользователя (после «Разрешить» FatSecret вернёт его на `/api/health/fatsecret/callback`).
3. Накатите миграцию (`alembic upgrade head`, выполняется в `entrypoint.sh`) — добавляет `users.fs_access_token` и `users.fs_access_secret`.
4. Каждый из вас отправляет боту `/link_fatsecret`, открывает ссылку и нажимает «Разрешить». `/unlink_fatsecret` отвязывает, `/nutrition` — итоги за сегодня.

Планировщик забирает КБЖУ всех привязанных пользователей в 07/11/15/19:00 и в 23:50 (с пушем «Итоги питания») по `FATSECRET_TZ` и делает UPSERT в `daily_nutrition` по `(user_id, date)`.

## Основные эндпоинты

| Метод и путь | Кто | Назначение |
|---|---|---|
| `GET /api/users/me` | любой | свой профиль |
| `GET/POST/PATCH/DELETE /api/users` | admin | белый список, роли |
| `GET /api/users/overview` | admin | сводка по всем участникам |
| `/api/transactions`, `/api/income`, `/api/limits`, `/api/reminders`, `/api/vacations`, `/api/upload`, `/api/dashboard-stats` | любой | только свои данные (admin: `target_user_id` / `all_users`) |
| `GET/POST /api/health/weight`, `GET /api/health/nutrition`, `GET /api/health/activity` | любой | только свои данные |
| `POST /api/health/webhook/weight` | Home Assistant | вес + пуш в Telegram |
| `GET /api/health/fatsecret/callback` | FatSecret (редирект браузера) | завершение привязки аккаунта |

## Тесты

```bash
pytest
```
Тесты `test_security.py` и `test_schema.py` проверяют правила доступа и схему (привязку всех таблиц к `users.id`,
уникальные индексы `(user_id, date)`); `test_migrations_chain.py` — линейность цепочки миграций.
