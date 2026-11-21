FROM python:3.12-slim

WORKDIR /app

COPY requirements-bot.txt ./
RUN python -m venv /app/.venv \
 && /app/.venv/bin/pip install --no-cache-dir --upgrade pip \
 && /app/.venv/bin/pip install --no-cache-dir -r requirements-bot.txt

# `python`/`pip` dentro do container passam a ser os do venv: sem isso,
# `docker compose exec <agente> python ...` usa o Python da imagem base, que
# nao tem requests/matplotlib (falha parcial, so quando pede grafico).
ENV PATH="/app/.venv/bin:$PATH"

COPY common ./common
COPY agenda ./agenda
COPY analista ./analista
COPY .opencode/agents ./.opencode/agents
COPY docker-entrypoint.sh /app/docker-entrypoint.sh

RUN chmod +x /app/docker-entrypoint.sh \
 && mkdir -p /app/.state \
 && chown -R 1000:1000 /app

ENTRYPOINT ["/app/docker-entrypoint.sh"]