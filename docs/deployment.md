# Развёртывание: LXC + Docker

## 1. LXC-контейнер (Proxmox)

Docker внутри LXC работает в **непривилегированном** контейнере, если включить вложенность:

```
# /etc/pve/lxc/<CTID>.conf
features: keyctl=1,nesting=1
```

Рекомендации: Debian 12 / Ubuntu 24.04, 2 vCPU, 2 ГБ RAM (pandas + pdfplumber + reportlab прожорливы при
разборе PDF), 10+ ГБ диска. Статический IP в LAN (DHCP-резервация) — на него завязаны `WEB_BASE_URL`
и Home Assistant. Часовой пояс контейнера не важен: приложение считает время в UTC и `Europe/Moscow` явно.

Установка Docker внутри LXC:
```bash
curl -fsSL https://get.docker.com | sh
```

## 2. Код и настройки

```bash
git clone https://github.com/Ermuk-code/home-finance.git /opt/lifeos
cd /opt/lifeos
cp .env.example .env
nano .env
```

### Переменные окружения

| Переменная | Обязательна | Описание |
|---|---|---|
| `BOT_TOKEN` | да | Токен от @BotFather |
| `ADMIN_TELEGRAM_ID`, `ADMIN_NAME` | да | Владелец (role=admin) |
| `PARTNER_TELEGRAM_ID`, `PARTNER_NAME` | нет | Второй участник (role=user) |
| `POSTGRES_USER/PASSWORD/DB` | да | Учётка БД (те же значения использует контейнер postgres) |
| `POSTGRES_HOST`, `POSTGRES_PORT` | да | `db`, `5432` внутри compose |
| `JWT_SECRET_KEY` | да | Длинная случайная строка: `openssl rand -hex 32` |
| `JWT_ALGORITHM` | нет | `HS256` |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD` | да | Вход в веб-панель по паролю; пароль можно задать bcrypt-хешем (`$2b$…`) |
| `WEB_BASE_URL` | да | Адрес панели, каким его видит браузер: `http://192.168.1.50:8000` |
| `APP_BIND_IP` | да для LAN | На каком IP публикуется порт 8000. По умолчанию `127.0.0.1` — **из LAN недоступно**. Для LAN: IP контейнера или `0.0.0.0` |
| `HEALTH_WEBHOOK_TOKEN` | нет | Секрет вебхука веса; пусто — вебхук выключен (503) |
| `WEIGHT_ADMIN_DIGEST` | нет | `true` — admin получает «партнёр взвесился» без цифр |
| `FATSECRET_CONSUMER_KEY/SECRET` | нет | Интеграция питания; пусто — выключена |
| `FATSECRET_CALLBACK_URL` | нет | По умолчанию `WEB_BASE_URL/api/health/fatsecret/callback` |
| `FATSECRET_TZ` | нет | `Europe/Moscow` — граница «сегодня» для питания |

## 3. Запуск

```bash
docker compose up -d --build
docker compose logs -f app
```

`entrypoint.sh` при каждом старте делает: `alembic upgrade head` → `python -m app.users.seed` → `python -m app.main`.

Проверка: `curl http://<IP>:8000/health` → `{"status":"ok"}`, в браузере `http://<IP>:8000/login`.

## 4. Обновление

```bash
cd /opt/lifeos
git pull
docker compose up -d --build
```

Код берётся **из образа**, поэтому после `git pull` обязательно `--build` (простой `restart` подхватит старый код).
Приложение в контейнере работает от пользователя `lifeos` (uid 1000), у сервиса `app` есть `HEALTHCHECK` —
состояние видно в `docker compose ps`.

Режим разработки с монтированием кода с хоста:
```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
```

Версии библиотек закреплены в `requirements.txt`. Чтобы обновить: поднять версию, прогнать `pytest`, пересобрать образ.

## 5. Бэкапы

Сервис `backup` в `docker-compose.yml` (скрипт [`scripts/backup.sh`](../scripts/backup.sh)) раз в сутки делает
`pg_dump` в каталог `./backups` рядом с проектом и удаляет дампы старше 14 дней. При старте сразу делает дамп,
если за сегодня его ещё нет.

| Переменная `.env` | По умолчанию | Описание |
|---|---|---|
| `BACKUP_HOUR_UTC` | `0` | Час запуска по UTC (0 = 03:00 МСК) |
| `BACKUP_KEEP_DAYS` | `14` | Сколько дней хранить дампы |

Журнал: `docker compose logs backup`. Ручной дамп прямо сейчас:
```bash
docker compose run --rm -e BACKUP_ONCE=1 backup
```

Восстановление (остановите `app`, чтобы никто не писал в БД):
```bash
docker compose stop app
docker compose exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists' < backups/lifeos_YYYY-MM-DD_HHMM.dump
docker compose start app
```

> ⚠️ `./backups` лежит в том же LXC, что и база: от удаления данных или битой миграции защищает, от смерти диска — нет.
> Копируйте каталог наружу: смонтируйте в LXC папку с NAS (Proxmox `mp0:`) и укажите её в `volumes` сервиса
> `backup`, или настройте `rsync`/`rclone` по cron. Дополнительно — бэкапы самого LXC в Proxmox (vzdump).

## 6. Локальная разработка и тесты

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements-dev.txt
ruff check app tests migrations
pytest
```

Тесты не требуют БД: `tests/conftest.py` подставляет фиктивные переменные окружения.
Те же проверки и сборку Docker-образа запускает GitHub Actions (`.github/workflows/ci.yml`) на каждый push в `main`
и pull request.
