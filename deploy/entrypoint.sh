#!/bin/sh
# Two processes in one container, which is a compromise the single-image layout buys.
#
# The compromise has one real hazard and this script exists to close it: if uvicorn dies,
# nginx keeps answering, keeps serving the SPA, and the container looks perfectly healthy
# while every request to /api returns 502. So neither process is allowed to outlive the
# other - whichever exits first takes the container down, and the orchestrator restarts it.
set -eu

UVICORN_WORKERS="${UVICORN_WORKERS:-2}"

terminate() {
    # Ask both to stop, then let the container exit. `kill` on an already-dead pid is not an
    # error worth failing on, hence the guards.
    [ -n "${api_pid:-}" ] && kill "$api_pid" 2>/dev/null || true
    [ -n "${nginx_pid:-}" ] && kill "$nginx_pid" 2>/dev/null || true
}
trap terminate INT TERM

uvicorn app.main:app \
    --host 127.0.0.1 \
    --port 8000 \
    --workers "$UVICORN_WORKERS" \
    --proxy-headers \
    --forwarded-allow-ips '*' &
api_pid=$!

nginx -g 'daemon off;' &
nginx_pid=$!

# Polled rather than `wait -n`, which is a bash builtin and this image runs dash. A second of
# latency on a process that has already died costs nothing; a script that silently does
# nothing under /bin/sh would cost the whole guard.
while kill -0 "$api_pid" 2>/dev/null && kill -0 "$nginx_pid" 2>/dev/null; do
    sleep 1
done

terminate
exit 1
