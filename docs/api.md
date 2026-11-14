# REST API и веб-страницы

Базовый адрес: `http://<IP LXC>:8000`. Интерактивная документация FastAPI: `/docs` (Swagger) и `/redoc`.

**Авторизация**: cookie `access_token` или `Authorization: Bearer <jwt>` — см. [architecture.md](architecture.md#аутентификация-веб-панели).

**Область данных** (эндпоинты с `get_scope`): по умолчанию — свои данные. Только для `admin`:
`?target_user_id=<id>` — профиль другого пользователя, `?all_users=true` — все без фильтра.
Пользователь `user` с чужим `target_user_id` получает 403, обращение к чужой записи по id — 404.

## Системное и вход
| Метод | Путь | Кто | Описание |
|---|---|---|---|
| GET | `/health` | все | Liveness: `{"status":"ok"}` |
| POST | `/api/login` | admin | form: `username`, `password` → cookie + токен |
| GET | `/api/login/telegram?token=` | из бота | Вход по ссылке `/web`, редирект на `/` |
| POST | `/api/logout` | все | Удаляет cookie |

## Пользователи — `/api/users`
| Метод | Путь | Кто | Описание |
|---|---|---|---|
| GET | `/api/users/me` | любой | Свой профиль |
| GET | `/api/users/overview` | admin | Сводка по всем участникам |
| GET | `/api/users` | admin | Белый список |
| POST | `/api/users` | admin | Добавить пользователя (telegram_id, name, role) |
| PATCH | `/api/users/{id}` | admin | Имя / роль (нельзя разжаловать владельца и последнего admin) |
| DELETE | `/api/users/{id}` | admin | Удалить вместе со всеми данными (нельзя владельца и себя) |

## Операции — `/api/transactions`
| Метод | Путь | Описание |
|---|---|---|
| GET | `/api/transactions` | Список с фильтрами и пагинацией |
| PATCH | `/api/transactions/{id}` | Изменить (категорию и т.п.) |
| DELETE | `/api/transactions/{id}` | Удалить |
| POST | `/api/transactions/manual` | Ручная операция / наличные |
| GET | `/api/transactions/categories` | Справочник категорий |
| POST | `/api/transactions/categories` | Новая категория |
| GET | `/api/transactions/sources` | Источники: наличные, карты, банки из операций |

## Загрузка файлов
| Метод | Путь | Описание |
|---|---|---|
| POST | `/api/upload` | multipart: `file`, опц. `user_id` (только admin), `bank_hint`. Сначала пробуется банковская выписка, затем брокерский отчёт. 422 — формат не распознан |

## Доходы — `/api/income`
| Метод | Путь | Описание |
|---|---|---|
| GET | `/api/income/analysis` | Разбивка доходов по источникам за период |
| GET | `/api/income/source/{source}` | Операции одного источника |
| GET | `/api/income/sources` | Список источников |
| GET / POST | `/api/income/rules` | Правила «ключевое слово → источник» |
| DELETE | `/api/income/rules/{id}` | Удалить правило |

## Лимиты, напоминания, отпуска
| Метод | Путь | Описание |
|---|---|---|
| GET | `/api/limits` | Лимиты и траты текущего месяца по категориям |
| PUT | `/api/limits/{category_id}` | Задать / снять лимит |
| GET / POST | `/api/reminders` | Напоминания (с повтором) |
| DELETE | `/api/reminders/{id}` | Удалить |
| GET / POST | `/api/vacations` | Периоды отпусков |
| DELETE | `/api/vacations/{id}` | Удалить |

## Дашборд, вклады, портфель (`app/web/pages.py`)
| Метод | Путь | Описание |
|---|---|---|
| GET | `/api/dashboard-stats?start_date=&end_date=&include_transfers=` | Всё для главной: доход/расход, категории, банки, наличные, вклады, портфель с MOEX |
| POST | `/api/deposits` | Добавить вклад/накопительный счёт (+ напоминание о закрытии) |
| PATCH | `/api/deposits/{id}/balance` | Новый остаток накопительного счёта |
| DELETE | `/api/deposits/{id}` | Удалить вклад |
| POST | `/api/assets` | Добавить/обновить позицию (ticker, amount, average_buy_price) |
| DELETE | `/api/assets/{ticker}` | Удалить позицию |
| GET | `/api/portfolio/report.pdf` | PDF-досье портфеля |
| GET | `/api/portfolio/prompt.txt` | Текстовый промпт для ИИ-анализа портфеля |

## Здоровье — `/api/health`
| Метод | Путь | Кто | Описание |
|---|---|---|---|
| GET | `/api/health/weight?limit=` | любой | История веса |
| POST | `/api/health/weight` | любой | `{"weight_kg": 82.4, "bmi": 24.1, "timestamp": "…"}` |
| GET | `/api/health/nutrition?limit=` | любой | Суточные КБЖУ |
| GET | `/api/health/activity?limit=` | любой | Активность (пока всегда пусто) |
| POST | `/api/health/webhook/weight` | Home Assistant | Заголовок `X-Webhook-Token`; тело `{"profile_alias": "admin", "weight_kg": 82.4}`. Коды: 503 нет токена в .env, 401 неверный токен, 404/409 профиль не найден/не уникален |
| GET | `/api/health/fatsecret/callback` | браузер после FatSecret | Завершение OAuth-привязки |

## HTML-страницы
`/login`, `/` (дашборд), `/transactions`, `/upload`, `/income`, `/reminders`, `/limits`, `/users` (только admin).
Без входа — редирект на `/login`. **Страниц для веса и питания пока нет.**
