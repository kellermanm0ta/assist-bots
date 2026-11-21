# AGENTS.md

Telegram assistant bots. The first agent is **agenda**: creates, lists and
cancels reminders from **text or voice**, with recurrence and a scheduler that
notifies at the right time. Same architecture as `persona-bots`: environment
config, native long-polling, `data/<agent>/` state, one container per agent.
There is **no test suite, linter or build step** — verification is running the
pieces directly.

## Repository layout

One package per agent, shared capabilities at the root:

```
common/     config, logger, Telegram client, audio, LLM client — no business rules
agenda/     everything that knows what a "lembrete" is
analista/   (next agent) business analysis over SQLite
```

Each agent package has a `config.py` that calls `common.config.iniciar(<agente>)`
on import and a `bot.py` with `main()`. The entrypoint is always
`python -m <agente>`; modules import across packages by absolute path
(`from common import telegram_api`), never the other agent's package.

## Architecture

```mermaid
flowchart LR
    TG["agenda/bot.py<br/>long-polling"] --> TXT["agenda/parser.py<br/>deterministico"]
    TG --> AUD["common/transcricao.py<br/>OpenRouter Gemini"]
    AUD --> TXT
    TXT -.->|"falhou"| RT["agenda/runtime.py<br/>LLM fallback"]
    TXT --> LEM["agenda/lembretes.py<br/>lembretes.json + Lock"]
    RT --> LEM
    RT --> LLM["common/llm.py<br/>cliente JSON"]
    AGD["agenda/agendador.py<br/>thread + rrule"] --> LEM
    AGD --> API["common/telegram_api.py<br/>send"]
```

- `parser.interpretar()` is deterministic and never persists; it returns a
  `Resultado` with the resolved absolute datetime. `runtime.interpretar()` is
  only called on failure and its fields go through `parser.de_campos()`.
- `lembretes.py` owns the JSON store (atomic tmp + `os.replace`, `RLock`).
- `agendador.py` is a daemon thread woken by `threading.Event`; sleep is
  clamped to 30 s. On due it sends, then computes `rrule.after()`.
- Catch-up: `TOLERANCIA_MIN`; late one-shots notify once, overdue recurring
  occurrences are skipped to the next future one.
- Dates/times are **naive local** (`TIMEZONE`), so wall-clock reminders stay
  fixed across DST.

## Configuration (environment)

| Var | Meaning | Default |
|---|---|---|
| `BOT_AGENT` | package to run (`python -m <BOT_AGENT>`); loads `.opencode/agents/<BOT_AGENT>.md`; logger name | package name |
| `BOT_NAME` | display name used in greetings (overrides `bot_name:`) | `BOT_AGENT` capitalized |
| `TIMEZONE` | zone for all schedules | `America/Sao_Paulo` |
| `STATE_DIR` | agent state (`lembretes.json`, `bot.log`, `bot.pid`) | `data/<BOT_AGENT>` |
| `HORA_PADRAO` | time for "todos os dias" shortcut | `08:00` |
| `TOLERANCIA_MIN` | max lateness still notified | `120` |
| `AUDIO_ENABLED` | accept voice/audio messages | `true` |
| `TRANSCRIBE_MODEL` | OpenRouter multimodal model | `google/gemini-2.5-flash` |
| `TRANSCRIBE_MAX_SEG` / `AUDIO_MAX_MB` | audio limits | `120` / `10` |
| `LLM_FALLBACK` | LLM when deterministic parsing fails | `true` |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_IDS` | secret (env_file); allowed list is required — without it the bot refuses every chat | — |
| `OPENROUTER_API_KEY` | secret (`.secrets/openrouter.env`) | — |

`BOT_AGENT` is deliberately **not** called `AGENT`: the ambient environment of
CI and agent runners often sets `AGENT=1`, which silently selected the wrong
secret directory.

With no `OPENROUTER_API_KEY`, set `AUDIO_ENABLED=false` and `LLM_FALLBACK=false`;
text + deterministic parser still work.

## Layout

```
common/                          shared: config, log, Telegram, audio, LLM
agenda/                          agenda agent (bot, parser, lembretes, agendador)
.opencode/agents/<agent>.md      persona/tone; `model:` optional (V2 frontmatter)
.secrets/openrouter.env          shared OpenRouter key (audio + fallback)
.secrets/<agent>/telegram.env    one bot/token per agent
data/<agent>/                    state: lembretes.json, bot.log, bot.pid
docker-compose.prod.yml          VPS deploy: GHCR image, no build
.github/workflows/               ci, publish (GHCR), deploy (SSH), retention
```

`common/config.py` loads `.secrets/openrouter.env` at import and
`.secrets/<BOT_AGENT>/telegram.env` (plus `.secrets/telegram.env` fallback)
inside `iniciar()`, so `./.venv/bin/python -m agenda` works if the files exist.

## Core scripts

Agent packages run as `python -m <agente>` (host: `./.venv/bin/python -m <agente>`).

`common/`:
- `config.py` — `BASE`, `iniciar(agente)`, env helpers (`flag`/`inteiro`/`texto`/
  `caminho`), secret loading, `agora()`, `TZ` (`ZoneInfo`).
- `telegram_api.py` — Bot API client: `api`, `send`, `send_photo`,
  `send_document`, `typing`, `download_file`, `editar`/`responder_callback`
  (inline buttons), `escape`, `redact`; token read lazily so import order never
  matters.
- `transcricao.py` — OpenRouter chat completions with `input_audio` base64
  (`ogg`); prompt fixed to literal pt-BR transcription.
- `llm.py` — OpenRouter JSON client (`chamar_json`), agent frontmatter reader,
  `bot_name()`/`model_for()`.
- `logsetup.py` — stdlib logging to stdout; logger `<BOT_AGENT>.*`.

`agenda/`:
- `config.py` — `HORA_PADRAO`, `TOLERANCIA_MIN`, `LEMBRETES_FILE`; re-exports
  the common knobs (`TZ`, `AUDIO_ENABLED`, ...) so the package keeps `config.X`.
- `bot.py` — long-poll (`message` + `callback_query`); commands
  `/start` `/help` `/id` `/listar` `/cancelar` `/apagar_inativos` (preview with
  Apagar/Cancelar buttons); text and `voice`/`audio` messages processed in
  worker threads (one message at a time per chat).
- `parser.py` — deterministic NL → `Resultado` (`criar`/`cancelar`/`erro`),
  `de_campos()` validation for the LLM fallback, `formatar()`/`descrever()`.
- `lembretes.py` — JSON store CRUD; `buscar_por_texto()` = exact → substring →
  `difflib` fuzzy; soft-delete keeps history; `apagar_inativos()` purges
  `ativo: false` for good (scoped to one chat when `chat_id` is given).
- `agendador.py` — scheduler thread (`Agendador`); `acordar()` on create/cancel.
- `runtime.py` — LLM fallback contract for reminders (JSON object, revalidated).

## Data model (`data/<agent>/lembretes.json`)

```json
[{"id":"a1b2c3","chat_id":123,"texto":"tomar remedio","rrule":"FREQ=DAILY",
  "primeira":"2026-11-18T08:00:00","proxima":"2026-11-18T08:00:00",
  "ativo":true,"origem":"audio","transcricao":"me lembre todos os dias ...",
  "criado":"2026-11-17T20:12:00","ultimo_envio":null,
  "perdido_notificado":false,"cancelado_em":null,"concluido_em":null}]
```

`ativo=false` (cancel/conclude) is never notified; the scheduler re-reads the
record right before sending.

## Docker (one container per agent)

- `Dockerfile` — `python:3.12-slim` + venv; deps `requests`, `python-dateutil`,
  `tzdata`. No ffmpeg/whisper: audio goes to OpenRouter.
- `docker-entrypoint.sh` — `exec`s `python -m $BOT_AGENT` as PID 1, so the same
  image serves every agent; `BOT_AGENT` comes from the compose `environment`.
- `docker-compose.yml` — `x-agente` anchor; non-secret config in `environment`,
  secrets via `env_file`, state via `./data/<agent>:/app/.state`.
- `docker-compose.prod.yml` — VPS variant: `image: ghcr.io/...` (no `build`),
  `TAG` from `.env`; same env/volume. See README "Deploy" for CI/CD.

## Verification (no test suite)

```bash
./.venv/bin/python -m compileall -q common agenda
./.venv/bin/python -c "import agenda.parser; from datetime import datetime; \
 print(agenda.parser.interpretar('me lembre em 18/11 as 8h de tomar remedio, repetir todos os dias', datetime(2026,11,17,22,0)))"
./.venv/bin/python -m common.transcricao teste.ogg
./.venv/bin/python -m agenda          # sobe o polling de verdade (nao rode com o bot vivo)
docker compose up -d --build && docker compose logs -f agenda
```

Nunca valide imports rodando `python -m agenda` com o token real: ele inicia o
long-polling e a instancia viva leva `409 Conflict`. Use
`python -c "import agenda.bot"` para verificar o grafo de imports.

## Notes

- `.gitignore` excludes `data/`, `.venv/`, `.secrets/*` (only `*.example` kept),
  `__pycache__/`, `.env`.
- **Never** run two instances with the same token — Telegram returns `409
  Conflict`.
- Restarts reload `lembretes.json`; recurring reminders are recomputed from
  their stored `proxima`, so no notification is lost (within tolerance).
- Secrets live only in GitHub Secrets; the deploy renders `.secrets/*.env` on
  the runner and overwrites the VPS copy every run, so editing those files on
  the VPS does not persist. Rotate with `gh secret set` + `gh workflow run
  deploy.yml` (see README, "Rotacao de chave").