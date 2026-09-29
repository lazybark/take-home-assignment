#!/bin/sh
# Container entrypoint. With arguments, runs them (e.g. `uv run pytest`);
# otherwise starts the service in APP_MODE=prod (gunicorn, the default) or dev (live reload).
set -eu

if [ "$#" -gt 0 ]; then
  exec "$@"
fi

PORT="${PORT:-8080}"
case "${APP_MODE:-prod}" in
  dev)
    # Werkzeug reloader: restarts the process on file changes in the bind mount.
    # Note: a reload kills in-flight requests exactly like a crash would.
    exec flask --app payments.app:create_app run --host 0.0.0.0 --port "$PORT" --reload --no-debugger
    ;;
  prod)
    # Converge on restart: one reconcile when the worker starts (not a timer).
    export RECONCILE_ON_START="${RECONCILE_ON_START:-true}"
    # One worker so a restart is one clean "crash"; threads serve concurrent terminals.
    # --timeout must exceed the maximum deadlineSeconds (120).
    exec gunicorn --workers 1 --threads 32 --timeout 180 --graceful-timeout 30 \
      --bind "0.0.0.0:$PORT" --access-logfile - "payments.app:create_app()"
    ;;
  *)
    echo "Unknown APP_MODE '${APP_MODE}' (expected dev or prod)" >&2
    exit 64
    ;;
esac
