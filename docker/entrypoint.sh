#!/bin/sh
set -e
case "$1" in
  api)
    # Retry migrations while Postgres finishes starting.
    i=0
    until alembic upgrade head; do
      i=$((i + 1))
      [ "$i" -ge 15 ] && echo "migrations failed after $i attempts" >&2 && exit 1
      sleep 2
    done
    exec python -m carscout.web
    ;;
  worker)
    exec python -m carscout.worker
    ;;
  *)
    exec "$@"
    ;;
esac
