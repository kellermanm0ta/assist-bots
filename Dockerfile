FROM python:3.12-slim

WORKDIR /app

COPY requirements-bot.txt ./
RUN python -m venv /app/.venv \
 && /app/.venv/bin/pip install --no-cache-dir --upgrade pip \
 && /app/.venv/bin/pip install --no-cache-dir -r requirements-bot.txt

COPY config.py logsetup.py telegram_api.py lembretes.py parser.py agendador.py \
     transcricao.py runtime.py telegram_bot.py ./
COPY .opencode/agents ./.opencode/agents
COPY docker-entrypoint.sh /app/docker-entrypoint.sh

RUN chmod +x /app/docker-entrypoint.sh \
 && mkdir -p /app/.state \
 && chown -R 1000:1000 /app

ENTRYPOINT ["/app/docker-entrypoint.sh"]