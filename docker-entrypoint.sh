#!/usr/bin/env bash
set -euo pipefail
umask 077
cd /app

# A mesma imagem serve todos os agentes: `BOT_AGENT` escolhe o pacote a executar.
exec /app/.venv/bin/python -m "${BOT_AGENT:?BOT_AGENT nao definido}"