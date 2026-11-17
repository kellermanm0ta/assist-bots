#!/usr/bin/env python3
"""Fallback de interpretacao via LLM (OpenRouter).

So e chamado quando o parser deterministico nao entende a frase. O modelo
devolve JSON e os campos passam por `parser.de_campos()`, que aplica as mesmas
validacoes (data no futuro, hora valida, rrule RFC5545). Persona/tom ficam em
`.opencode/agent/<AGENT>.md`.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import requests

import config
import logsetup
import parser as P

BASE = Path(__file__).resolve().parent
AGENT_DIR = BASE / ".opencode" / "agent"
DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"
API = "https://openrouter.ai/api/v1/chat/completions"

log = logsetup.get_logger("runtime")

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)

_CONTRATO = """Voce converte frases de lembrete em JSON.

Agora: {agora} (fuso {tz}).
Responda APENAS um objeto JSON, sem markdown, no formato:
{{"acao": "criar" | "cancelar" | "nenhuma",
  "texto": "evento em poucas palavras",
  "data": "YYYY-MM-DD" | null,
  "hora": "HH:MM" | null,
  "rrule": "REGRA_RFC5545" | null}}

Regras:
- Datas relativas ("amanha", "sexta") devem ser resolvidas para data absoluta.
- "todos os dias" -> "FREQ=DAILY"; "todas as tercas" -> "FREQ=WEEKLY;BYDAY=TU";
  "todo dia 5" -> "FREQ=MONTHLY;BYMONTHDAY=5"; "a cada 2 horas" -> "FREQ=HOURLY;INTERVAL=2".
- Sem recorrencia, rrule = null.
- "cancelar X" -> acao "cancelar" e texto "X".
- Se nao for um pedido de lembrete, acao "nenhuma".
- Nao invente dados ausentes."""


def load_agent(name: str) -> dict:
    path = AGENT_DIR / f"{name}.md"
    if not path.exists():
        return {"meta": {}, "prompt": ""}
    text = path.read_text(encoding="utf-8")
    m = _FM_RE.match(text)
    if not m:
        return {"meta": {}, "prompt": text.strip()}
    meta: dict = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line[:1].isspace():
            key, val = line.split(":", 1)
            meta[key.strip()] = val.strip()
    return {"meta": meta, "prompt": m.group(2).strip()}


def bot_name() -> str:
    meta = load_agent(config.AGENT)["meta"]
    return meta.get("bot_name") or config.AGENT.capitalize()


def model_for(name: str) -> str:
    val = os.environ.get(f"OPENROUTER_MODEL_{name.upper()}", "").strip()
    if not val:
        val = os.environ.get("OPENROUTER_MODEL", "").strip()
    if not val:
        val = load_agent(name)["meta"].get("model", "").strip()
    val = re.sub(r"^openrouter/", "", val)
    return val or DEFAULT_MODEL


def _chave() -> str:
    return os.environ.get("OPENROUTER_API_KEY", "").strip()


def interpretar(mensagem: str, agora) -> P.Resultado | None:
    if not config.LLM_FALLBACK:
        return None
    chave = _chave()
    if not chave:
        log.warning("LLM_FALLBACK ligado sem OPENROUTER_API_KEY; seguindo sem fallback")
        return None

    persona = load_agent(config.AGENT)["prompt"]
    sistema = (persona + "\n\n" if persona else "") + _CONTRATO.format(
        agora=agora.isoformat(sep=" ", timespec="minutes"), tz=config.TIMEZONE
    )
    payload = {
        "model": model_for(config.AGENT),
        "temperature": 0,
        "messages": [
            {"role": "system", "content": sistema},
            {"role": "user", "content": mensagem},
        ],
        "response_format": {"type": "json_object"},
    }
    obj = _chamar(payload, chave, mensagem)
    if obj is None:
        return None

    acao = str(obj.get("acao") or "").strip().lower()
    if acao == "cancelar":
        alvo = str(obj.get("texto") or "").strip()
        if alvo:
            return P.Resultado("cancelar", texto=alvo)
        return None
    if acao != "criar":
        return None

    res = P.de_campos(
        obj.get("texto"),
        obj.get("data"),
        obj.get("hora"),
        obj.get("rrule"),
        agora,
    )
    if res.tipo != "criar":
        log.warning("fallback LLM devolveu campos invalidos: %r", obj)
        return None
    log.info("fallback LLM resolveu %r -> %s", mensagem[:80], res.primeira)
    return res


def _chamar(payload: dict, chave: str, mensagem: str) -> dict | None:
    headers = {"Authorization": f"Bearer {chave}", "Content-Type": "application/json"}
    for tentativa in (1, 2):
        try:
            resp = requests.post(API, headers=headers, json=payload, timeout=60)
            if resp.status_code == 400 and "response_format" in payload:
                # modelo sem JSON mode: tenta sem o parametro
                payload = {k: v for k, v in payload.items() if k != "response_format"}
                continue
            if resp.status_code >= 400:
                log.warning("OpenRouter HTTP %s no fallback: %s", resp.status_code, resp.text[:200])
                return None
            texto = resp.json()["choices"][0]["message"]["content"] or ""
            return _json(texto)
        except Exception as e:
            log.warning("falha no fallback (tentativa %d): %r", tentativa, e)
    return None


def _json(texto: str) -> dict | None:
    texto = texto.strip()
    if texto.startswith("```"):
        texto = re.sub(r"^```[a-zA-Z]*\s*", "", texto)
        texto = re.sub(r"\s*```$", "", texto)
    m = re.search(r"\{.*\}", texto, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


if __name__ == "__main__":
    import sys

    from datetime import datetime

    agora = datetime.now(config.TZ).replace(tzinfo=None) if config.TZ else datetime.now()
    print(interpretar(" ".join(sys.argv[1:]), agora))