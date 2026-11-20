#!/usr/bin/env python3
"""Armazenamento dos lembretes em JSON (atômico) com trava de thread.

O bot (polling) e o agendador (thread) mexem no mesmo arquivo, por isso toda
operacao passa por um `RLock` e a escrita e feita em arquivo temporario +
`os.replace`. O volume de dados e pequeno; JSON simples basta (sem banco).
"""
from __future__ import annotations

import copy
import difflib
import json
import secrets
import threading
from datetime import datetime

import config
import logsetup
from parser import normalizar

log = logsetup.get_logger("lembretes")

_lock = threading.RLock()
_cache: list | None = None


def agora_iso() -> str:
    return _agora().isoformat(timespec="seconds")


def _agora() -> datetime:
    return datetime.now(config.TZ).replace(tzinfo=None) if config.TZ else datetime.now()


def _load() -> list:
    global _cache
    if _cache is None:
        try:
            if config.LEMBRETES_FILE.exists():
                _cache = json.loads(config.LEMBRETES_FILE.read_text(encoding="utf-8"))
            else:
                _cache = []
        except Exception as e:
            log.error("falha ao ler %s: %r", config.LEMBRETES_FILE, e)
            _cache = []
        if not isinstance(_cache, list):
            _cache = []
    return _cache


def _save() -> None:
    try:
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = config.LEMBRETES_FILE.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(_cache or [], ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(config.LEMBRETES_FILE)
    except Exception as e:
        log.error("falha ao salvar %s: %r", config.LEMBRETES_FILE, e)


def _novo_id() -> str:
    existentes = {item.get("id") for item in _load()}
    while True:
        lid = secrets.token_hex(3)
        if lid not in existentes:
            return lid


def listar(chat_id=None, incluir_inativos: bool = False) -> list[dict]:
    with _lock:
        itens = [
            item
            for item in _load()
            if (incluir_inativos or item.get("ativo"))
            and (chat_id is None or item.get("chat_id") == chat_id)
        ]
        itens.sort(key=lambda x: x.get("proxima") or "")
        return copy.deepcopy(itens)


def ativos() -> list[dict]:
    return listar(incluir_inativos=False)


def vencidos(agora: datetime) -> list[dict]:
    limite = agora.isoformat(timespec="seconds")
    with _lock:
        return copy.deepcopy(
            [
                item
                for item in _load()
                if item.get("ativo")
                and item.get("proxima")
                and item["proxima"] <= limite
            ]
        )


def obter(lid: str) -> dict | None:
    with _lock:
        for item in _load():
            if item.get("id") == lid:
                return copy.deepcopy(item)
    return None


def adicionar(
    chat_id: int,
    texto: str,
    rrule_str: str | None,
    primeira: datetime,
    origem: str = "texto",
    transcricao: str | None = None,
) -> dict:
    with _lock:
        item = {
            "id": _novo_id(),
            "chat_id": chat_id,
            "texto": texto,
            "rrule": rrule_str,
            "primeira": primeira.isoformat(timespec="seconds"),
            "proxima": primeira.isoformat(timespec="seconds"),
            "ativo": True,
            "origem": origem,
            "transcricao": transcricao,
            "criado": agora_iso(),
            "ultimo_envio": None,
            "perdido_notificado": False,
            "cancelado_em": None,
            "concluido_em": None,
        }
        _load().append(item)
        _save()
        log.info(
            "lembrete %s criado chat=%s rrule=%s primeira=%s texto=%r",
            item["id"],
            chat_id,
            rrule_str,
            item["primeira"],
            texto,
        )
        return copy.deepcopy(item)


def cancelar(lid: str) -> dict | None:
    with _lock:
        for item in _load():
            if item.get("id") == lid and item.get("ativo"):
                item["ativo"] = False
                item["cancelado_em"] = agora_iso()
                _save()
                log.info("lembrete %s cancelado (%r)", lid, item.get("texto"))
                return copy.deepcopy(item)
    return None


def concluir(lid: str) -> None:
    with _lock:
        for item in _load():
            if item.get("id") == lid:
                item["ativo"] = False
                item["concluido_em"] = agora_iso()
                _save()
                return


def apagar_inativos(chat_id=None) -> int:
    """Remove de vez os lembretes com `ativo: false` (cancelados/concluidos).

    Sem `chat_id` limpa todos os chats; com `chat_id` mexe so nos daquele chat.
    Devolve quantos registros foram removidos.
    """
    global _cache
    with _lock:
        itens = _load()
        restantes = [
            item
            for item in itens
            if item.get("ativo")
            or (chat_id is not None and item.get("chat_id") != chat_id)
        ]
        removidos = len(itens) - len(restantes)
        if not removidos:
            return 0
        _cache = restantes
        _save()
        log.info("apagados %d lembretes inativos (chat=%s)", removidos, chat_id)
        return removidos


def registrar_envio(lid: str, quando_iso: str | None = None) -> None:
    with _lock:
        for item in _load():
            if item.get("id") == lid:
                item["ultimo_envio"] = quando_iso or agora_iso()
                _save()
                return


def marcar_perdido(lid: str) -> None:
    with _lock:
        for item in _load():
            if item.get("id") == lid:
                item["perdido_notificado"] = True
                _save()
                return


def reagendar(lid: str, proxima_iso: str) -> None:
    with _lock:
        for item in _load():
            if item.get("id") == lid:
                item["proxima"] = proxima_iso
                item["perdido_notificado"] = False
                _save()
                return


def buscar_por_id(chat_id: int, prefixo: str) -> list[dict]:
    prefixo = (prefixo or "").strip().lstrip("#").lower()
    if not prefixo:
        return []
    with _lock:
        return copy.deepcopy(
            [
                item
                for item in _load()
                if item.get("chat_id") == chat_id
                and item.get("ativo")
                and str(item.get("id", "")).startswith(prefixo)
            ]
        )


def buscar_por_texto(chat_id: int, termo: str) -> list[dict]:
    """Casa por igualdade, depois substring (nos dois sentidos), depois fuzzy."""
    alvo = normalizar(termo)
    if not alvo:
        return []
    with _lock:
        ativos_chat = [
            item
            for item in _load()
            if item.get("chat_id") == chat_id and item.get("ativo")
        ]

    def nome(item: dict) -> str:
        return normalizar(item.get("texto", ""))

    exatos = [item for item in ativos_chat if nome(item) == alvo]
    if exatos:
        return copy.deepcopy(exatos)

    parciais = [
        item
        for item in ativos_chat
        if alvo in nome(item) or (nome(item) and nome(item) in alvo)
    ]
    if parciais:
        return copy.deepcopy(parciais)

    indice = {nome(item): item for item in ativos_chat}
    proximos = difflib.get_close_matches(alvo, list(indice), n=5, cutoff=0.8)
    return copy.deepcopy([indice[n] for n in proximos])