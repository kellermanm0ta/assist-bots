#!/usr/bin/env python3
"""Configuracao comum aos agentes, lida de variaveis de ambiente (12-factor).

Aqui ficam apenas as capacidades que qualquer agente usa: raiz do projeto, fuso,
diretorio de estado, carga de segredos e os parametros de audio/LLM. As regras
de negocio de cada agente (hora padrao, tolerancia, caminho da base) ficam em
`<agente>/config.py`, que chama `iniciar()` no import.

`AGENT`, `TIMEZONE`, `TZ` e `STATE_DIR` so ganham valor depois de `iniciar()`;
por isso todo modulo de agente importa o proprio `config` antes de `logsetup`
e `telegram_api`.
"""
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Raiz do projeto: no container /app, no host o proprio repositorio.
BASE = Path(__file__).resolve().parent.parent

# Preenchidos por iniciar().
AGENT = ""
TIMEZONE = ""
TZ = None
STATE_DIR = BASE / "data"


def load_env_file(path: Path) -> None:
    """Le `CHAVE=valor` de um .env sem sobrescrever o que ja esta no ambiente."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip())


# Chave da OpenRouter (transcricao de audio + fallback de interpretacao).
# Independe do agente, entao pode ser carregada no import.
load_env_file(BASE / ".secrets" / "openrouter.env")


def flag(name: str, default: bool = False) -> bool:
    val = os.environ.get(name, "").strip().lower()
    if not val:
        return default
    return val in ("1", "true", "yes", "on")


def inteiro(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def texto(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def caminho(name: str, default: str) -> Path:
    p = Path(os.environ.get(name, default).strip() or default)
    return p if p.is_absolute() else (BASE / p)


def iniciar(agente: str) -> None:
    """Fixa o agente da instancia e carrega os segredos dele.

    Chamado pelo `<agente>/config.py` no import. `AGENT` do ambiente vence
    (e o que o docker-compose define); sem ela, vale o nome do pacote.
    """
    global AGENT, TIMEZONE, TZ, STATE_DIR

    AGENT = os.environ.get("BOT_AGENT", "").strip() or agente
    os.environ["BOT_AGENT"] = AGENT  # normaliza para logger e subprocessos

    load_env_file(BASE / ".secrets" / AGENT / "telegram.env")
    load_env_file(BASE / ".secrets" / "telegram.env")

    TIMEZONE = texto("TIMEZONE", "America/Sao_Paulo")
    try:
        TZ = ZoneInfo(TIMEZONE)
    except ZoneInfoNotFoundError:  # sem tzdata no host: cai no fuso local
        TZ = None

    STATE_DIR = caminho("STATE_DIR", f"data/{AGENT}")


def agora() -> datetime:
    """Agora no fuso configurado, naive (wall-clock local)."""
    return datetime.now(TZ).replace(tzinfo=None) if TZ else datetime.now()


# --- Capacidades comuns: audio e LLM -------------------------------------

AUDIO_ENABLED = flag("AUDIO_ENABLED", True)
TRANSCRIBE_MODEL = texto("TRANSCRIBE_MODEL", "google/gemini-2.5-flash")
TRANSCRIBE_MAX_SEG = inteiro("TRANSCRIBE_MAX_SEG", 120)
AUDIO_MAX_MB = inteiro("AUDIO_MAX_MB", 10)

LLM_FALLBACK = flag("LLM_FALLBACK", True)