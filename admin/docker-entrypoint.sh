#!/usr/bin/env sh
set -e

# Honour ADMIN_HOST / ADMIN_PORT overrides (the client derives the api-server
# as rendezvous_port - 2, i.e. 5187 -> 5185, so 5185 is the default).
HOST="${ADMIN_HOST:-0.0.0.0}"
PORT="${ADMIN_PORT:-5185}"

exec uvicorn app.main:app --host "${HOST}" --port "${PORT}"
