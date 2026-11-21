#!/usr/bin/env python3
"""Parametros do analista. O que e de infraestrutura vive em `common/config.py`.

Importar este modulo ja inicializa o agente (`iniciar("analista")`), portanto
todo modulo do pacote deve importa-lo antes de qualquer outro import nosso.
"""
import common.config as base

base.iniciar("analista")

AGENT = base.AGENT
TIMEZONE = base.TIMEZONE
TZ = base.TZ
STATE_DIR = base.STATE_DIR

# Capacidades comuns que os modulos do analista consomem como `config.X`.
AUDIO_ENABLED = base.AUDIO_ENABLED
TRANSCRIBE_MODEL = base.TRANSCRIBE_MODEL
TRANSCRIBE_MAX_SEG = base.TRANSCRIBE_MAX_SEG
AUDIO_MAX_MB = base.AUDIO_MAX_MB
LLM_FALLBACK = base.LLM_FALLBACK

# Artefato gerado por `python -m analista.ingestao` (fora do container).
# O bot so abre em modo read-only; nunca escreve nem importa a ingestao.
ANALISTA_DB = base.caminho("ANALISTA_DB", str(STATE_DIR / "analista.db"))

# Limites das consultas e do contexto.
MAX_LINHAS = base.inteiro("ANALISTA_MAX_LINHAS", 5000)
TIMEOUT_SQL = base.inteiro("ANALISTA_TIMEOUT_SQL", 10)
HISTORICO_TURNOS = base.inteiro("HISTORICO_TURNOS", 8)

# Graficos.
GRAFICO_ENABLED = base.flag("GRAFICO_ENABLED", True)
GRAFICO_DIR = base.caminho("GRAFICO_DIR", str(STATE_DIR / "graficos"))
GRAFICO_MAX_PONTOS = base.inteiro("GRAFICO_MAX_PONTOS", 60)
GRAFICO_MAX_FATIAS = base.inteiro("GRAFICO_MAX_FATIAS", 10)

# Relogio do fuso configurado (naive), usado por periodo/ferramentas.
agora = base.agora