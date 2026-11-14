# Внешние интеграции

## Home Assistant → вес

Умные весы передают замер в Home Assistant, автоматизация HA вызывает `rest_command`, который шлёт вес в Life OS.

1. В `.env`: `HEALTH_WEBHOOK_TOKEN=<результат openssl rand -hex 32>`, `APP_BIND_IP=<IP LXC или 0.0.0.0>`.
2. В HA `secrets.yaml`: `life_os_webhook_token: "<тот же токен>"`.
3. Добавить [`homeassistant/rest_command.yaml`](../homeassistant/rest_command.yaml) (заменить `<IP_КОНТЕЙНЕРА>`).
4. В автоматизацию весов добавить вызов из [`homeassistant/automation_snippet.yaml`](../homeassistant/automation_snippet.yaml):
   `profile` — кто взвесился (`admin`, `user`, имя, id или telegram_id), `weight` — вес в кг.

```
POST /api/health/webhook/weight
X-Webhook-Token: <HEALTH_WEBHOOK_TOKEN>
Content-Type: application/json

{"profile_alias": "admin", "weight_kg": 82.4, "bmi": 24.1}
```

Ответ `200 {"status":"ok","notified":true,...}`. Владелец замера получает пуш с разницей к прошлому замеру.

## FatSecret → питание

FatSecret отдаёт дневник питания только по **OAuth 1.0a** (3-legged). Токены пользователя бессрочные.

1. Создать приложение на platform.fatsecret.com, взять Consumer Key / Shared Secret (OAuth 1.0) → `.env`.
2. `WEB_BASE_URL` должен открываться в браузере телефона/ПК, на котором нажимают «Разрешить»
   (в LAN это работает, пока вы дома; снаружи — только через VPN).
3. В боте: `/link_fatsecret` → ссылка → войти в FatSecret → «Разрешить» → callback сохраняет токены в `users`.
4. Синхронизация: 07/11/15/19:00 и 23:50 (с пушем итогов), плюс по `/nutrition`.

Ожидающие привязки (request token) хранятся в памяти процесса 15 минут — после рестарта ссылку нужно запросить заново.

## MOEX ISS → котировки и купоны

Публичный API `https://iss.moex.com/iss` без ключа. Используется для текущих цен, купонов, дат погашения,
доходности облигаций и дивидендов. Запросы выполняются при каждом открытии дашборда / `/portfolio`
(таймаут 10 с, кеширования нет). Если MOEX недоступен, позиции показываются без текущей цены.

## Telegram

Long polling к `api.telegram.org`. Нужен только исходящий доступ в интернет из LXC.
