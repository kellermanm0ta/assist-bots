# assist-bots

Bots de assistencia no Telegram. O primeiro agente e a **agenda**: cria, lista e
cancela lembretes enviados por **texto ou audio**, com notificacao na hora certa
e recorrencias (diaria, semanal, mensal, por intervalo).

Mesma arquitetura do `persona-bots`: configuracao por ambiente, long-polling
nativo, estado em `data/<agente>/` e um container por agente.

## Como funciona

```
telegram_bot.py (long-polling)
   |  texto  -> parser.py (deterministico)  --falhou--> runtime.py (LLM)
   |  audio  -> transcricao.py (OpenRouter Gemini) -> parser.py
   v
lembretes.py (data/<agente>/lembretes.json, escrita atomica + Lock)
   ^
   |  proxima ocorrencia (dateutil.rrule)
agendador.py (thread) --venceu--> telegram_api.send("⏰ ...")
```

- **Parser deterministico** entende os formatos documentados abaixo, sem custo
  de API. O LLM (`LLM_FALLBACK=true`) so entra quando o parser falha, e os
  campos devolvidos sao revalidados por `parser.de_campos()`.
- **Audio** e transcrito pela OpenRouter (`TRANSCRIBE_MODEL`, padrao
  `google/gemini-2.5-flash`); o texto transcrito e ecoado para voce conferir.
- **Recorrencia** usa RFC5545 (`dateutil.rrule`). Datas/horas sao locais
  (`TIMEZONE`), entao "as 8h" continua "as 8h" mesmo com mudanca de horario.
- **Catch-up**: se o bot estava fora no horario, notifica atrasado dentro de
  `TOLERANCIA_MIN`; recorrentes pulam ocorrencias vencidas e vao para a proxima.

## Frases aceitas

```
me lembre em 18/11 as 8h de tomar remedio
me lembre em 18/11 as 8h de tomar remedio, repetir todos os dias
me lembre em 18/11 as 8h de academia todas as tercas
me lembre todos os dias de tomar remedio          # 08:00, a partir de amanha
me lembre todas as tercas as 7h de academia
me lembre todo dia 5 de pagar o aluguel
me lembre a cada 2 horas de alongar
me lembre em 18 de novembro as 20h de jantar
cancelar tomar remedio
```

Datas: `hoje`, `amanha`, `depois de amanha`, `18/11`, `18/11/2026`,
`18 de novembro`, `dia 18`, `proxima sexta`. Horas: `8h`, `8:30`, `8h30`,
`20 horas`, `meio-dia`, `meia-noite`.

Recorrencia: `repetir todos os dias`, `todos os dias`, `todas as [dia]`,
`toda semana`, `todo dia 5`, `todo ano`, `a cada N horas/dias/semanas/meses`,
`de segunda a sexta`.

## Comandos

`/start`, `/help`, `/id`, `/listar`, `/cancelar <id|evento>`,
`/apagar_inativos [confirmar]`.

Cancelamento por nome casa por igualdade, substring e semelhanca. Se houver mais
de um lembrete parecido, o bot lista os ids e pede `/cancelar <id>` — nunca
cancela em massa.

`/apagar_inativos` remove de vez do `lembretes.json` o historico com
`"ativo": false` (cancelados e concluidos). Ele mostra a previa com dois botoes
inline — **Apagar** e **Cancelar** — entao nao precisa digitar nada;
`/apagar_inativos confirmar` continua valendo como atalho por texto. So apaga os
lembretes do proprio chat.

## Layout

```
.opencode/agents/agenda.md       persona/tom do agente
.secrets/openrouter.env          chave da OpenRouter (compartilhada)
.secrets/agenda/telegram.env     token do bot (um por agente)
data/agenda/                     lembretes.json, bot.log, bot.pid
```

**Nunca comite `.secrets/` nem `data/`**: o primeiro guarda a chave e o token,
o segundo guarda `chat_id`, lembretes e transcricoes pessoais. Ambos ja estao no
`.gitignore`; nao use `git add -f`.

## Configuracao (environment)

| Var | Significado | Default |
|---|---|---|
| `AGENT` | carrega `.opencode/agents/<AGENT>.md` e nomeia o logger | `agenda` |
| `BOT_NAME` | nome exibido nas saudacoes (sobrepoe `bot_name:`) | `AGENT` capitalizado |
| `TIMEZONE` | fuso dos horarios | `America/Sao_Paulo` |
| `STATE_DIR` | lembretes.json / log / pid | `data/<AGENT>` |
| `HORA_PADRAO` | hora do atalho "todos os dias" | `08:00` |
| `TOLERANCIA_MIN` | atraso maximo para notificar | `120` |
| `AUDIO_ENABLED` | aceita notas de voz/audio | `true` |
| `TRANSCRIBE_MODEL` | modelo multimodal da OpenRouter | `google/gemini-2.5-flash` |
| `TRANSCRIBE_MAX_SEG` / `AUDIO_MAX_MB` | limites do audio | `120` / `10` |
| `LLM_FALLBACK` | usa LLM quando o parser falha | `true` |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_IDS` | secret (env_file); a lista e obrigatoria — sem ela o bot recusa todo mundo | — |
| `OPENROUTER_API_KEY` | secret (`.secrets/openrouter.env`) | — |

Sem `OPENROUTER_API_KEY`, use `AUDIO_ENABLED=false` e `LLM_FALLBACK=false`: o bot
continua funcionando por texto com o parser deterministico.

## Rodar com Docker (recomendado)

```bash
cp .secrets/openrouter.env.example .secrets/openrouter.env
mkdir -p .secrets/agenda data/agenda
cp .secrets/telegram.env.example .secrets/agenda/telegram.env
chmod 600 .secrets/openrouter.env .secrets/agenda/telegram.env

docker compose up -d --build
docker compose logs -f agenda
```

## Deploy (GitHub Actions → GHCR → VPS)

Pipeline: `push main` → **ci** valida → **publish** publica
`ghcr.io/kellermanm0ta/assist-bots:{latest,sha-xxxx}` → **deploy** puxa e sobe na
VPS por SSH. O pacote e **publico**, entao a VPS nao precisa autenticar no registry.

Segredos (GitHub → Settings → Secrets and variables → Actions):

| Secret | Uso |
|---|---|
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_IDS` | `.secrets/agenda/telegram.env` na VPS (a lista e obrigatoria) |
| `OPENROUTER_API_KEY` | `.secrets/openrouter.env` na VPS |
| `VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY` | acesso SSH (`VPS_PORT` opcional) |

VPS (setup unico): usuario `deploy` no grupo `docker`, Docker Engine + compose v2,
chave SSH, e `~/assist-bots/` com `data/agenda/` e `.secrets/agenda/`. O deploy
grava essas pastas e um `.env` com a `TAG` da imagem.

Primeiro deploy: rode o pipeline uma vez. Se o `pull` na VPS falhar com `401`
(pacote privado), torne-o público (GitHub → Packages → assist-bots → Package
settings → Change visibility); em repo público o pacote costuma já sair público.

Arquivos: `docker-compose.prod.yml` e `.github/workflows/{ci,publish,deploy,retention}.yml`.

Rollback: Actions → **deploy** → Run workflow, informando `tag` (ex: `sha-ab12cd3`).

### Rotacao de chave (OpenRouter, Telegram)

O arquivo na VPS e **copia derivada** do GitHub Secret: o deploy o reescreve
inteiro a cada run. Editar `.secrets/*.env` na VPS a mao nao sobrevive ao
proximo deploy — rotacione sempre pelo secret.

1. Crie a chave nova no provedor e **mantenha a antiga ativa**.
2. Grave o valor no secret (nunca como caminho, sem aspas e sem newline):

   ```bash
   gh secret set OPENROUTER_API_KEY     # prompt interativo: cole o valor puro
   ```

3. Dispare o deploy sem precisar de commit:

   ```bash
   gh workflow run deploy.yml
   gh run watch "$(gh run list --workflow=deploy --limit 1 \
     --json databaseId -q '.[0].databaseId')"
   ```

4. Confira o que o container esta usando (fingerprint, nunca o valor):

   ```bash
   cd ~/assist-bots
   grep -c '^OPENROUTER_API_KEY=sk-or-v1-' .secrets/openrouter.env   # 1 = ok
   docker compose -f docker-compose.prod.yml exec agenda \
     sh -c 'printf %s "$OPENROUTER_API_KEY" | sha256sum'
   ```

   O log do deploy imprime o mesmo fingerprint (`chars` + `sha256` curto) no
   passo **Validar segredos**, entao da para comparar sem expor nada.

5. Valide o uso (audio ou frase ambigua) e so entao revogue a chave antiga.

O passo **Validar segredos** aborta o deploy antes de qualquer SSH quando um
secret esta vazio, parece caminho (`/...`, `~/...`), tem espaco/quebra de linha
ou foge do formato esperado — em vez de subir um container com a chave errada.

Para forcar a recriacao do container apos a rotacao:

```bash
docker compose -f docker-compose.prod.yml up -d --force-recreate
```

## Rodar no host (sem Docker)

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements-bot.txt
nohup ./run_bot.sh >/dev/null 2>&1 </dev/null &
kill "$(cat data/agenda/bot.pid)"      # parar
```

## Verificacao

Nao ha suite de testes. Verificacao direta:

```bash
./.venv/bin/python -c "import parser; from datetime import datetime; \
 print(parser.interpretar('me lembre todos os dias de tomar remedio', datetime(2026,11,17,22,0)))"
./.venv/bin/python transcricao.py teste.ogg
./.venv/bin/python telegram_bot.py
```

`data/agenda/lembretes.json` guarda tudo; no Docker os logs vao para
`docker logs`, no host para `data/agenda/bot.log`.