#!/usr/bin/env python3
"""Fallback de interpretacao da agenda via LLM (OpenRouter).

So e chamado quando o parser deterministico nao entende a frase. O modelo
devolve JSON e os campos passam por `parser.de_campos()`, que aplica as mesmas
validacoes (data no futuro, hora valida, rrule RFC5545). O cliente HTTP e a
persona ficam em `common/llm.py` e `.opencode/agents/<AGENT>.md`.
"""
from __future__ import annotations

from agenda import config  # noqa: F401  (inicia o agente antes do logger)
from agenda import parser as P
from common import llm, logsetup

log = logsetup.get_logger("runtime")

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


def interpretar(mensagem: str, agora) -> P.Resultado | None:
    if not config.LLM_FALLBACK:
        return None
    if not llm.chave():
        log.warning("LLM_FALLBACK ligado sem OPENROUTER_API_KEY; seguindo sem fallback")
        return None

    persona = llm.load_agent(config.AGENT)["prompt"]
    sistema = (persona + "\n\n" if persona else "") + _CONTRATO.format(
        agora=agora.isoformat(sep=" ", timespec="minutes"), tz=config.TIMEZONE
    )
    payload = {
        "model": llm.model_for(config.AGENT),
        "temperature": 0,
        "messages": [
            {"role": "system", "content": sistema},
            {"role": "user", "content": mensagem},
        ],
        "response_format": {"type": "json_object"},
    }
    obj = llm.chamar_json(payload, descricao="fallback agenda")
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


if __name__ == "__main__":
    import sys

    from common.config import agora

    print(interpretar(" ".join(sys.argv[1:]), agora()))