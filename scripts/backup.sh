#!/bin/sh
# Ежедневный бэкап PostgreSQL (сервис `backup` в docker-compose.yml).
#
# Раз в сутки в BACKUP_HOUR_UTC делает pg_dump в формате custom (-Fc) в /backups и удаляет дампы
# старше BACKUP_KEEP_DAYS. При старте сразу делает дамп, если за сегодня его ещё нет.
#   BACKUP_ONCE=1 — сделать один дамп и выйти (ручной запуск: docker compose run --rm -e BACKUP_ONCE=1 backup)
set -eu

: "${POSTGRES_USER:?}" "${POSTGRES_PASSWORD:?}" "${POSTGRES_DB:?}"
export PGPASSWORD="$POSTGRES_PASSWORD"
PGHOST="${POSTGRES_HOST:-db}"
PGPORT="${POSTGRES_PORT:-5432}"
BACKUP_DIR="${BACKUP_DIR:-/backups}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
HOUR_UTC="${BACKUP_HOUR_UTC:-0}"   # 0 UTC = 03:00 МСК

log() { echo "$(date -u '+%Y-%m-%d %H:%M:%S') [backup] $*"; }

dump() {
    stamp="$(date -u '+%Y-%m-%d_%H%M')"
    target="$BACKUP_DIR/lifeos_${stamp}.dump"
    tmp="$target.tmp"
    if pg_dump -h "$PGHOST" -p "$PGPORT" -U "$POSTGRES_USER" -Fc "$POSTGRES_DB" > "$tmp" && [ -s "$tmp" ]; then
        mv "$tmp" "$target"
        log "OK: $target ($(du -h "$target" | cut -f1))"
    else
        rm -f "$tmp"
        log "ОШИБКА: pg_dump не удался"
        return 1
    fi
    find "$BACKUP_DIR" -name 'lifeos_*.dump' -type f -mtime +"$KEEP_DAYS" -print -delete | sed 's/^/удалён старый: /'
}

has_today() {
    ls "$BACKUP_DIR"/lifeos_"$(date -u '+%Y-%m-%d')"_*.dump >/dev/null 2>&1
}

mkdir -p "$BACKUP_DIR"

if [ "${BACKUP_ONCE:-0}" = "1" ]; then
    dump
    exit $?
fi

log "Старт: ежедневно в ${HOUR_UTC}:00 UTC, хранение ${KEEP_DAYS} дн., каталог $BACKUP_DIR"
has_today || dump || true

while true; do
    sleep 600
    hour="$(date -u '+%H' | sed 's/^0//')"
    if [ "${hour:-0}" -eq "$HOUR_UTC" ] && ! has_today; then
        dump || true
    fi
done
