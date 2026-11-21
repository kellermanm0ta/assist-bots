#!/usr/bin/env python3
"""Parametros da agenda. O que e de infraestrutura vive em `common/config.py`.

Importar este modulo ja inicializa o agente (`iniciar("agenda")`), portanto
todo modulo do pacote deve importa-lo antes de qualquer outro import nosso.
"""
import common.config as base

base.iniciar("agenda")

# Reexportados para os modulos do pacote usarem `config.AGENT`, `config.TZ`, ...
AGENT = base.AGENT
TIMEZONE = base.TIMEZONE
TZ = base.TZ
STATE_DIR = base.STATE_DIR

# Capacidades comuns que os modulos da agenda consomem como `config.X`.
AUDIO_ENABLED = base.AUDIO_ENABLED
TRANSCRIBE_MODEL = base.TRANSCRIBE_MODEL
TRANSCRIBE_MAX_SEG = base.TRANSCRIBE_MAX_SEG
AUDIO_MAX_MB = base.AUDIO_MAX_MB
LLM_FALLBACK = base.LLM_FALLBACK

# Relogio do fuso configurado (naive), compartilhado pelos modulos.
agora = base.agora

LEMBRETES_FILE = STATE_DIR / "lembretes.json"

# Atalho "me lembre todos os dias de X": hora padrao e inicio no dia seguinte.
HORA_PADRAO = base.texto("HORA_PADRAO", "08:00")
# Quanto tempo depois do horario ainda vale notificar um lembrete (minutos).
TOLERANCIA_MIN = base.inteiro("TOLERANCIA_MIN", 120)