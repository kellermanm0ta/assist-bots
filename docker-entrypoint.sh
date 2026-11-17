#!/usr/bin/env bash
set -euo pipefail
cd /app

exec /app/.venv/bin/python telegram_bot.py