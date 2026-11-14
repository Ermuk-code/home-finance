# Модель данных

PostgreSQL 16, ORM — SQLAlchemy 2 (async, asyncpg). Схема управляется **только Alembic** (`migrations/`).
Деньги хранятся в `NUMERIC` (Decimal), даты операций — `timestamptz`.

## Схема связей

```
users ─┬─< transactions >── categories ──< category_limits >── users
       ├─< assets
       ├─< deposits ──< reminders (deposit_id, ON DELETE CASCADE)
       ├─< reminders
       ├─< vacations
       ├─< income_rules
       ├─< category_limits
       ├─< weight_logs
       ├─< daily_nutrition   (уникально по user_id + date)
       └─< activity_logs     (уникально по user_id + date)
```

Все `user_id` — `FOREIGN KEY users.id ON DELETE CASCADE`: удаление пользователя удаляет все его данные.

## Таблицы

### `users` — участники
| Поле | Тип | Примечание |
|---|---|---|
| id | int PK | |
| telegram_id | bigint, unique | ключ входа в бот |
| name | varchar(100) | используется и как `profile_alias` вебхука веса |
| role | varchar(16) | `admin` / `user` (CHECK) |
| created_at | timestamptz | |
| broker_cash_rub | numeric(14,2) null | свободные рубли из последнего брокерского отчёта |
| fs_access_token, fs_access_secret | varchar(255) null | постоянные токены FatSecret (хранятся в открытом виде) |

### Финансы
| Таблица | Ключевые поля | Смысл |
|---|---|---|
| `categories` | name (unique), type `income`/`expense`, keywords[], is_mandatory | Общий справочник (15 категорий сидируются автоматически) |
| `category_limits` | user_id, category_id, monthly_limit; unique(user_id, category_id) | Персональные месячные лимиты |
| `transactions` | user_id, category_id null, amount (±), description, date, bank_name, is_parsed_automatically | Операции. Доход > 0, расход < 0. Наличные — `bank_name = 'Наличные'` |
| `assets` | user_id, ticker, asset_type `stock`/`bond`/`fund`, amount, average_buy_price null, isin, name | Позиции брокерского портфеля |
| `deposits` | user_id, bank_name, title, amount, interest_rate, kind `deposit`/`savings`, opened_at, term_months, end_date, capitalization | Вклады и накопительные счета |
| `reminders` | user_id, title, description, trigger_date, is_notified, recurrence, recurrence_every, recurrence_day, deposit_id | Напоминания (сейчас живут в финансовом домене) |
| `vacations` | user_id, name, start_date, end_date | Периоды отпусков: траты в эти даты → «Путешествия» |
| `income_rules` | user_id, keyword, source | «Если в описании keyword — это источник source» (parents, salary, …) |

### Здоровье
| Таблица | Ключевые поля | Смысл |
|---|---|---|
| `weight_logs` | user_id, weight_kg numeric(5,2), bmi null, timestamp | Замеры веса (HA-вебхук или API) |
| `daily_nutrition` | user_id, date, calories, protein, fat, carbs; unique(user_id, date) | Суточные итоги КБЖУ из FatSecret |
| `activity_logs` | user_id, date, steps, active_calories; unique(user_id, date) | Активность — **таблица есть, но ничем не заполняется** |

## Миграции

Цепочка (линейная, проверяется `tests/test_migrations_chain.py`):

```
17b4ca61d956 initial_schema
 └ b5b67dd01555 deposits
   └ 130750653161 category limits, mandatory flag, vacations
     └ c3a91f7d2e44 deposit kind/term/capitalization
       └ d4e8b2a61f30 reminder ↔ deposit
         └ e5f1c9a37b20 broker report: isin, name, cash
           └ f6a2d8b41c93 reminder recurrence
             └ a8c3e5d7f102 recurrence_every
               └ b9d4f6e8a213 income_rules
                 └ 7c1d2e9f4a60 Life OS: роли, user_id везде, health-таблицы, персональные лимиты
                   └ 3e8a5c1d9b47 FatSecret tokens   ← head
```

Новая миграция:
```bash
alembic revision --autogenerate -m "short_name"
alembic upgrade head
```
После создания обновите ожидаемую голову в `tests/test_migrations_chain.py`.
