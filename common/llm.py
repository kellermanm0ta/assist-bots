#!/usr/bin/env python3
"""Camada LLM comum: frontmatter do agente e cliente JSON da OpenRouter.

Generico de proposito: cada agente define o proprio contrato de saida (o da
agenda vive em `agenda/runtime.py`). Persona/tom continuam em
`.opencode/agents/<AGENT>.md`.
"""
from __future__ import annotations

import json
import os
import re

import requests

from common import config, logsetup

log = logsetup.get_logger("llm")

AGENT_DIR = config.BASE / ".opencode" / "agents"
DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"
API = "https://openrouter.ai/api/v1/chat/completions"

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def load_agent(name: str) -> dict:
    """Le `.opencode/agents/<name>.md` -> {"meta": {...}, "prompt": "..."}."""
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


def bot_name(name: str | None = None) -> str:
    agente = name or config.AGENT
    val = os.environ.get("BOT_NAME", "").strip()
    if not val:
        val = load_agent(agente)["meta"].get("bot_name", "").strip()
    return val or agente.capitalize()


def model_for(name: str) -> str:
    val = os.environ.get(f"OPENROUTER_MODEL_{name.upper()}", "").strip()
    if not val:
        val = os.environ.get("OPENROUTER_MODEL", "").strip()
    if not val:
        val = load_agent(name)["meta"].get("model", "").strip()
    val = re.sub(r"^openrouter/", "", val)
    return val or DEFAULT_MODEL


def chave() -> str:
    return os.environ.get("OPENROUTER_API_KEY", "").strip()


def chamar_json(payload: dict, descricao: str = "") -> dict | None:
    """POST em /chat/completions esperando um objeto JSON de volta.

    Tenta duas vezes; se o modelo recusar `response_format`, repete sem ele.
    Devolve None em qualquer falha (o chamador decide o fallback).
    """
    chave_api = chave()
    if not chave_api:
        log.warning("OPENROUTER_API_KEY ausente; sem chamada de LLM")
        return None

    headers = {"Authorization": f"Bearer {chave_api}", "Content-Type": "application/json"}
    for tentativa in (1, 2):
        try:
            resp = requests.post(API, headers=headers, json=payload, timeout=60)
            if resp.status_code == 400 and "response_format" in payload:
                # modelo sem JSON mode: tenta sem o parametro
                payload = {k: v for k, v in payload.items() if k != "response_format"}
                continue
            if resp.status_code >= 400:
                log.warning("OpenRouter HTTP %s: %s", resp.status_code, resp.text[:200])
                return None
            texto = resp.json()["choices"][0]["message"]["content"] or ""
            return extrair_json(texto)
        except Exception as e:
            log.warning("falha na chamada LLM %s (tentativa %d): %r", descricao, tentativa, e)
    return None


def extrair_json(texto: str) -> dict | None:
    """Aceita JSON puro ou cercado por ```/texto; devolve so objetos."""
    texto = (texto or "").strip()
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