#!/usr/bin/env python3
"""Configuracao do bot, lida de variaveis de ambiente (12-factor).

O mesmo codigo serve para qualquer agente/instancia: cada container define
`AGENT`, `STATE_DIR`, `TIMEZONE`, etc. Segredos (token do Telegram, chave da
OpenRouter) chegam por `env_file`/`.secrets`. Ver AGENTS.md / README.md.
"""
import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

BASE = Path(__file__).resolve().parent


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip())


# Chave da OpenRouter (transcricao de audio + fallback de interpretacao).
_load_env_file(BASE / ".secrets" / "openrouter.env")


def _flag(name: str, default: bool = False) -> bool:
    val = os.environ.get(name, "").strip().lower()
    if not val:
        return default
    return val in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _path(name: str, default: str) -> Path:
    p = Path(os.environ.get(name, default).strip() or default)
    return p if p.is_absolute() else (BASE / p)


AGENT = os.environ.get("AGENT", "agenda").strip() or "agenda"

# Token do bot: por agente (Docker/host) e fallback na raiz de .secrets.
_load_env_file(BASE / ".secrets" / AGENT / "telegram.env")
_load_env_file(BASE / ".secrets" / "telegram.env")

TIMEZONE = os.environ.get("TIMEZONE", "America/Sao_Paulo").strip() or "America/Sao_Paulo"
try:
    TZ = ZoneInfo(TIMEZONE)
except ZoneInfoNotFoundError:  # sem tzdata no host: cai no fuso local
    TZ = None

STATE_DIR = _path("STATE_DIR", f"data/{AGENT}")
LEMBRETES_FILE = STATE_DIR / "lembretes.json"

# Atalho "me lembre todos os dias de X": hora padrao e inicio no dia seguinte.
HORA_PADRAO = os.environ.get("HORA_PADRAO", "08:00").strip() or "08:00"
# Quanto tempo depois do horario ainda vale notificar um lembrete (minutos).
TOLERANCIA_MIN = _int("TOLERANCIA_MIN", 120)

AUDIO_ENABLED = _flag("AUDIO_ENABLED", True)
TRANSCRIBE_MODEL = (
    os.environ.get("TRANSCRIBE_MODEL", "google/gemini-2.5-flash").strip()
    or "google/gemini-2.5-flash"
)
TRANSCRIBE_MAX_SEG = _int("TRANSCRIBE_MAX_SEG", 120)
AUDIO_MAX_MB = _int("AUDIO_MAX_MB", 10)

LLM_FALLBACK = _flag("LLM_FALLBACK", True)