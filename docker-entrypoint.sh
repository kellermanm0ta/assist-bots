#!/usr/bin/env bash
set -euo pipefail
umask 077
cd /app

exec /app/.venv/bin/python telegram_bot.py