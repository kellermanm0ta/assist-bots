#!/usr/bin/env python3
"""Contexto por chat: ultimas perguntas e o que foi consultado.

Fica em memoria (o processo e unico) e serve so para o LLM entender follow-ups
do tipo "e agora so da Equipe Vega?". Nada de dado sensivel vai para disco.
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field

from analista import config

_lock = threading.Lock()
_cache: dict[int, deque] = {}


@dataclass
class Turno:
    pergunta: str
    ferramenta: str = ""
    args: dict = field(default_factory=dict)


def registrar(chat_id: int, pergunta: str, ferramenta: str = "", args: dict | None = None) -> None:
    with _lock:
        fila = _cache.setdefault(chat_id, deque(maxlen=config.HISTORICO_TURNOS))
        fila.append(Turno(pergunta=pergunta, ferramenta=ferramenta, args=args or {}))


def historico(chat_id: int) -> list[Turno]:
    with _lock:
        return list(_cache.get(chat_id, ()))


def limpar(chat_id: int) -> None:
    with _lock:
        _cache.pop(chat_id, None)


def como_texto(chat_id: int, limite: int = 4) -> str:
    """Bloco compacto para o prompt: pergunta + ferramenta/args usados."""
    turnos = historico(chat_id)[-limite:]
    if not turnos:
        return ""
    linhas = []
    for turno in turnos:
        linha = f'- "{turno.pergunta}"'
        if turno.ferramenta:
            linha += f" -> {turno.ferramenta}({_args_texto(turno.args)})"
        linhas.append(linha)
    return "\n".join(linhas)


def _args_texto(args: dict) -> str:
    partes = []
    for chave, valor in args.items():
        if valor in (None, "", {}, []):
            continue
        partes.append(f"{chave}={valor}")
    return ", ".join(partes)