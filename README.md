# assist-bots

Bots de assistencia no Telegram. Dois agentes compartilham uma imagem e um
repositorio:

- **agenda** — cria, lista e cancela lembretes enviados por **texto ou audio**,
  com notificacao na hora certa e recorrencias (diaria, semanal, mensal, por
  intervalo).
- **analista** — analista de negocio sobre uma base **SQLite** de vendas,
  respondendo receita, custo, margem, rankings e evolucao, em texto e com
  graficos.

Mesma arquitetura do `persona-bots`: configuracao por ambiente, long-polling
nativo, estado em `data/<agente>/` e um container por agente.

## Estrutura do repositorio

Um pacote por agente; o que e comum fica em `common/` e nunca conhece regra de
negocio:

```
common/    config, log, cliente Telegram, audio, cliente LLM
agenda/    o que entende "lembrete": bot, parser, lembretes, agendador
analista/  o que entende "vendas": ingestao offline, tools SQL, graficos
```

Cada pacote tem `config.py` (chama `common.config.iniciar(<agente>)` no import) e
`bot.py` com `main()`. O entrypoint e sempre `python -m <agente>`; um agente
nunca importa o pacote do outro.

## Como funciona (agenda)

```
agenda/bot.py (long-polling)
   |  texto  -> agenda/parser.py (deterministico)  --falhou--> agenda/runtime.py
   |  audio  -> common/transcricao.py (OpenRouter) -> agenda/parser.py
   v
agenda/lembretes.py (data/<agente>/lembretes.json, escrita atomica + Lock)
   ^
   |  proxima ocorrencia (dateutil.rrule)
agenda/agendador.py (thread) --venceu--> common/telegram_api.send("⏰ ...")
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

## Analista de negocio

```
python -m analista.ingestao --csv <dir|arquivo>     # OFFLINE, no host
        |  contrato de 12 colunas -> data/analista/analista.db
        v
analista/bot.py (long-poll) -> interpretacao.py (LLM escolhe a tool)
        |                            |
        |                            +--> ferramentas.py -> SQLite (read-only)
        |                            +--> db.py (SELECT livre, com guardrails)
        v
   texto + tabela  +  PNG (matplotlib, so quando pedido)
```

### 1. Gerar a base (fora do container)

A ingestao e um comando separado, so stdlib, e nao roda no boot nem dentro do
container. O agente apenas abre o artefato em modo read-only.

```bash
./.venv/bin/python -m analista.ingestao \
  --csv ~/Downloads/relatorio-vendas-consolidado \
  --saida data/analista/analista.db
```

```
fontes: 1 arquivo(s)
  relatorio_final.csv               117085 linhas  4a70ada1f889
gravadas 117085 | duplicadas 0 | invalidas 0
periodo 2022-05-30 .. 2025-05-29 | 5 equipes | 24 vendedores | 15 servicos | 18988 clientes
receita 1.014.928.594,05 | custo 710.521.008,72 | margem 304.407.585,33 (29.99%)
db: .../data/analista/analista.db (31.1 MB)
```

- Escrita **atomica** (`<saida>.tmp` + `VACUUM` + `os.replace`): trocar a base
  nao exige reiniciar o bot e nunca deixa o arquivo pela metade.
- **Idempotente**: chave `(equipe, id_origem)` + `INSERT OR IGNORE`. Se o
  diretorio tem `relatorio_final.csv` (a uniao dos arquivos por equipe), so ele
  e lido; `--consolidado ""` le todos.
- O artefato se descreve (`meta`): `schema_version`, fontes com sha256, periodo
  e totais. Sem mudancas, a segunda execucao so avisa (use `--forcar`).
- `--inspecionar <db>` imprime o meta de um artefato existente.

Colunas canonicas (com aliases aceitos): `data_venda|data`, `Id|id`, `equipe`,
`vendedor`, `categoria_servico|categoria`, `servico|produto`, `quantidade`,
`preco_unitario|valor`, `custo`, `cliente`, `estado|uf`, `regiao`. Cabecalho
diferente falha **antes** de escrever qualquer byte.

### 2. Perguntar

Texto ou audio. O LLM escolhe a ferramenta e os argumentos; **quem calcula e o
SQLite** — o modelo nunca devolve numero.

```
qual foi a receita de 2024?
top 5 vendedores em 2024
comparar 2023 com 2024
margem por equipe no primeiro trimestre de 2024
evolucao mensal da receita em 2023 (grafico)
e so da Equipe Vega?
```

Periodos: `2024`, `marco de 2024`, `1o trimestre de 2023`, `ultimos 30 dias`,
`de 01/03 a 15/04`, `tudo`. Sem ano explicito, vale o **ultimo ano da base**
(e nao o ano corrente).

### 3. Comandos

`/base`, `/resumo <periodo>`, `/ranking <dimensao> <periodo> [top_n]`,
`/serie <metrica> <periodo>`, `/tabela <dimensao> <periodo>`, `/sql <select>`,
`/grafico <pergunta>`, `/pizza <pergunta>`, `/limpar`, `/id`.

Os comandos diretos nao chamam LLM (funcionam ate sem `OPENROUTER_API_KEY`) e
servem de teste rapido. Toda resposta traz os botoes **Grafico**, **Pizza**
(quando faz sentido), **Tabela** e **CSV**.

### 5. Graficos

| Tipo | Quando |
|---|---|
| `linha` | evolucao no tempo (serie) |
| `barra` | ranking e comparacao entre categorias |
| `comparacao` | dois periodos lado a lado |
| `pizza` / `rosca` | participacao, fatias, "share" |

A pizza agrega a cauda em **"Outros"** contra o total do periodo (padrao: 10
fatias, `GRAFICO_MAX_FATIAS`) e so aparece quando a metrica e somavel e os
valores sao positivos — pizza de ticket medio ou de margem % e recusada. A
legenda avisa quando as fatias cobrem pouco do total:

```
Quantidade por cliente | top 10 | ... | as 10 maiores somam 8.2% do total
```

Em `cliente` isso e o dado falando: 18.988 clientes, nenhum passa de ~1,4%, e a
fatia "Outros" domina. Em `categoria` (3) ou `regiao` (5) a pizza fecha 100% e
fica limpa.

### 4. Sem Telegram: CLI das tools

```bash
./.venv/bin/python -m analista.consultas info_base
./.venv/bin/python -m analista.consultas resumo periodo=2024
./.venv/bin/python -m analista.consultas ranking dimensao=vendedor periodo=2024 top_n=5
./.venv/bin/python -m analista.consultas serie metrica=receita granularidade=mensal --grafico /tmp/g.png
```

### Limites e leitura correta dos dados

- Nao existem compras, estoque nem devolucao: "entrada e saida" e receita x
  custo. Margem e **bruta**.
- `cliente` guarda sobrenomes repetidos, nao um cadastro: ranking de cliente e
  indicativo.
- `Vinicius` aparece em duas equipes e `Ana Julia`/`Ana Julia` com grafias
  diferentes; o agente avisa quando o recorte depende disso.
- SQL livre e ultimo recurso, com whitelist de views, `LIMIT` e timeout.

## Layout

```
common/                          config, log, Telegram, audio, LLM
agenda/                          agente da agenda
analista/                        agente do analista + ingestao offline
.opencode/agents/<agente>.md     persona/tom de cada agente
.secrets/openrouter.env          chave da OpenRouter (compartilhada)
.secrets/<agente>/telegram.env   token do bot (um por agente)
data/agenda/                     lembretes.json, bot.log, bot.pid
data/analista/                   analista.db (gerado offline) e graficos/
```

**Nunca comite `.secrets/` nem `data/`**: o primeiro guarda a chave e o token,
o segundo guarda `chat_id`, lembretes e transcricoes pessoais. Ambos ja estao no
`.gitignore`; nao use `git add -f`.

## Configuracao (environment)

| Var | Significado | Default |
|---|---|---|
| `BOT_AGENT` | pacote executado (`python -m <BOT_AGENT>`), carrega `.opencode/agents/<BOT_AGENT>.md` e nomeia o logger | nome do pacote |
| `BOT_NAME` | nome exibido nas saudacoes (sobrepoe `bot_name:`) | `BOT_AGENT` capitalizado |
| `TIMEZONE` | fuso dos horarios | `America/Sao_Paulo` |
| `STATE_DIR` | estado do agente (lembretes.json / log / pid) | `data/<BOT_AGENT>` |
| `HORA_PADRAO` | hora do atalho "todos os dias" | `08:00` |
| `TOLERANCIA_MIN` | atraso maximo para notificar | `120` |
| `AUDIO_ENABLED` | aceita notas de voz/audio | `true` |
| `TRANSCRIBE_MODEL` | modelo multimodal da OpenRouter | `google/gemini-2.5-flash` |
| `TRANSCRIBE_MAX_SEG` / `AUDIO_MAX_MB` | limites do audio | `120` / `10` |
| `LLM_FALLBACK` | usa LLM quando o parser falha | `true` |
| `ANALISTA_DB` | artefato SQLite lido pelo analista (read-only) | `$STATE_DIR/analista.db` |
| `GRAFICO_ENABLED` | gera PNG sob pedido | `true` |
| `GRAFICO_MAX_FATIAS` | fatias antes de agregar o resto em "Outros" | `10` |
| `ANALISTA_MAX_LINHAS` / `ANALISTA_TIMEOUT_SQL` | teto de linhas e timeout do SQL | `5000` / `10` |
| `HISTORICO_TURNOS` | contexto de follow-up por chat | `8` |
| `ANALISTA_CSV` | `--csv` padrao da ingestao offline | — |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_IDS` | secret (env_file); a lista e obrigatoria — sem ela o bot recusa todo mundo | — |
| `OPENROUTER_API_KEY` | secret (`.secrets/openrouter.env`) | — |

Sem `OPENROUTER_API_KEY`, use `AUDIO_ENABLED=false` e `LLM_FALLBACK=false`: o bot
continua funcionando por texto com o parser deterministico.

## Rodar com Docker (recomendado)

```bash
cp .secrets/openrouter.env.example .secrets/openrouter.env
mkdir -p .secrets/agenda data/agenda .secrets/analista data/analista
cp .secrets/telegram.env.example .secrets/agenda/telegram.env
cp .secrets/telegram.env.example .secrets/analista/telegram.env
chmod 600 .secrets/openrouter.env .secrets/agenda/telegram.env .secrets/analista/telegram.env

# base do analista: gerada no host, o container so monta
./.venv/bin/python -m analista.ingestao --csv ~/Downloads/relatorio-vendas-consolidado

docker compose up -d --build
docker compose logs -f agenda analista
```

Um bot/token por agente no @BotFather. **Nunca** use o mesmo token em dois
containers: o Telegram devolve `409 Conflict`.

## Deploy (GitHub Actions → GHCR → VPS)

Pipeline: `push main` → **ci** valida → **publish** publica
`ghcr.io/kellermanm0ta/assist-bots:{latest,sha-xxxx}` → **deploy** puxa e sobe na
VPS por SSH. O pacote e **publico**, entao a VPS nao precisa autenticar no registry.

Segredos (GitHub → Settings → Secrets and variables → Actions):

| Secret | Uso |
|---|---|
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_IDS` | `.secrets/agenda/telegram.env` na VPS (a lista e obrigatoria) |
| `ANALISTA_TELEGRAM_BOT_TOKEN`, `ANALISTA_TELEGRAM_ALLOWED_USER_IDS` | `.secrets/analista/telegram.env` na VPS |
| `OPENROUTER_API_KEY` | `.secrets/openrouter.env` na VPS |
| `VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY` | acesso SSH (`VPS_PORT` opcional) |

Os quatro secrets de bot sao obrigatorios: **Validar segredos** aborta antes do
SSH se algum faltar ou fugir do formato, em vez de subir um container sem token.
Os nomes sao distintos de proposito — gravar o token do analista em
`TELEGRAM_BOT_TOKEN` trocaria o token da agenda.

O `analista.db` **nao** passa pelo pipeline (e dado de cliente, e o pacote do
GHCR e publico). Copie uma vez, e de novo a cada atualizacao da base:

```bash
scp data/analista/analista.db \
  deploy@SEU_HOST:~/assist-bots/data/analista/analista.db
```

O agente le a base a cada consulta, entao a troca vale na hora, sem restart.

VPS (setup unico): usuario `deploy` no grupo `docker`, Docker Engine + compose v2,
chave SSH, e `~/assist-bots/` com `data/agenda/`, `data/analista/`,
`.secrets/agenda/` e `.secrets/analista/`. O deploy grava essas pastas e um
`.env` com a `TAG` da imagem.

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
./.venv/bin/python -m compileall -q common agenda analista
./.venv/bin/python -c "import agenda.parser; from datetime import datetime; \
 print(agenda.parser.interpretar('me lembre todos os dias de tomar remedio', datetime(2026,11,17,22,0)))"
./.venv/bin/python -m common.transcricao teste.ogg
./.venv/bin/python -m agenda    # sobe o polling (nao rode com o bot ja ativo)

./.venv/bin/python -m analista.ingestao --inspecionar data/analista/analista.db
./.venv/bin/python -m analista.consultas info_base
./.venv/bin/python -m analista.consultas resumo periodo=2024
./.venv/bin/python -m analista.consultas ranking dimensao=vendedor periodo=2024 top_n=5
```

Gabarito da base atual: **117.085 linhas**, receita `1.014.928.594,05`, custo
`710.521.008,72`, periodo `2022-05-30 .. 2025-05-29`.

`data/agenda/lembretes.json` guarda tudo; no Docker os logs vao para
`docker logs`, no host para `data/agenda/bot.log`.