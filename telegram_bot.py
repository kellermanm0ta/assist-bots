#!/usr/bin/env python3
"""Bot do Telegram da agenda: texto e audio -> lembretes recorrentes.

Long-polling nativo. O parser deterministico entende as frases; se falhar,
`runtime.interpretar()` (LLM) tenta resolver. As notificacoes ficam a cargo
da thread `agendador.Agendador`.
"""
import os
import re
import signal
import sys
import threading
import time
from datetime import datetime

import requests

import config
import lembretes
import logsetup
import parser
import runtime
import telegram_api as tg
import transcricao
from agendador import Agendador

log = logsetup.get_logger("bot")

ALLOWED = {
    int(x)
    for x in re.split(r"[,\s]+", os.environ.get("TELEGRAM_ALLOWED_USER_IDS", ""))
    if x.strip().isdigit()
}
LONG_POLL = 25

_busy: set = set()
_busy_lock = threading.Lock()
_agendador: Agendador | None = None


def agora_local() -> datetime:
    return datetime.now(config.TZ).replace(tzinfo=None) if config.TZ else datetime.now()


def build_help() -> str:
    return (
        f"Sou {runtime.bot_name()}, sua agenda. Escreva ou mande um audio.\n\n"
        "Exemplos:\n"
        "  me lembre em 18/11 as 8h de tomar remedio\n"
        "  me lembre em 18/11 as 8h de academia todas as tercas\n"
        "  me lembre todos os dias de tomar remedio\n"
        "  me lembre todas as tercas as 7h de academia\n"
        "  cancelar tomar remedio\n\n"
        "Comandos:\n"
        "  /listar   - lembretes ativos\n"
        "  /cancelar <id|evento> - cancela um lembrete\n"
        "  /apagar_inativos - apaga de vez o historico cancelado/concluido\n"
        "  /id       - mostra seu user id"
    )


HELP = build_help()


# ---------------------------------------------------------------------------
# Criacao / cancelamento
# ---------------------------------------------------------------------------

def _confirma_criacao(lem: dict) -> str:
    dt = datetime.fromisoformat(lem["proxima"])
    texto = f"Vou te lembrar em {parser.formatar(dt)}"
    if lem.get("rrule"):
        texto += f", {parser.descrever(lem['rrule'])}"
    return f"{texto}. (#{lem['id']})"


def _resumo(lem: dict) -> str:
    dt = datetime.fromisoformat(lem["proxima"])
    linha = f"#{lem['id']} - {lem['texto']} - {parser.formatar(dt)}"
    if lem.get("rrule"):
        linha += f" ({parser.descrever(lem['rrule'])})"
    return linha


def _cancelar(chat_id: int, alvo: str) -> str:
    if _agendador:
        _agendador.acordar()
    alvo = (alvo or "").strip()
    if not alvo:
        return "Uso: cancelar <evento> ou /cancelar <id>."

    por_id = lembretes.buscar_por_id(chat_id, alvo)
    if len(por_id) > 1:
        linhas = "\n".join(_resumo(item) for item in por_id)
        return "Mais de um id com esse prefixo. Use /cancelar <id>:\n" + linhas
    if len(por_id) == 1:
        item = lembretes.cancelar(por_id[0]["id"])
        if item:
            return f'Cancelado: "{item["texto"]}".'

    candidatos = lembretes.buscar_por_texto(chat_id, alvo)
    if not candidatos:
        return f'Nao achei lembrete parecido com "{alvo}".'
    if len(candidatos) == 1:
        item = lembretes.cancelar(candidatos[0]["id"])
        if not item:
            return "Nao consegui cancelar esse lembrete."
        extra = f" (era {parser.descrever(item['rrule'])})" if item.get("rrule") else ""
        return f'Cancelado: "{item["texto"]}"{extra}.'

    linhas = "\n".join(_resumo(item) for item in candidatos)
    return "Achei mais de um. Qual deles? Use /cancelar <id>:\n" + linhas


def _listar(chat_id: int) -> str:
    ativos = lembretes.listar(chat_id)
    if not ativos:
        return "Nenhum lembrete ativo."
    return "Lembretes ativos:\n" + "\n".join(_resumo(item) for item in ativos)


def _apagar_inativos(chat_id: int, arg: str = "") -> str:
    """Purga do store os lembretes cancelados/concluidos (ativo: false)."""
    inativos = [
        item
        for item in lembretes.listar(chat_id, incluir_inativos=True)
        if not item.get("ativo")
    ]
    if not inativos:
        return "Nenhum lembrete inativo para apagar."

    if arg.strip().lower() not in ("confirmar", "sim"):
        linhas = "\n".join(f"#{item['id']} - {item['texto']}" for item in inativos[:10])
        resto = len(inativos) - 10
        extra = f"\n... e mais {resto}." if resto > 0 else ""
        return (
            f"{len(inativos)} lembrete(s) inativo(s) serao apagados de vez:\n"
            f"{linhas}{extra}\n\n"
            "Confirme com /apagar_inativos confirmar"
        )

    removidos = lembretes.apagar_inativos(chat_id)
    return f"{removidos} lembrete(s) inativo(s) apagado(s)."


def _processar(chat_id: int, texto: str, reply_to=None, origem: str = "texto") -> None:
    agora = agora_local()
    resultado = parser.interpretar(texto, agora)
    if resultado.tipo == "erro":
        alternativa = runtime.interpretar(texto, agora)
        if alternativa is not None:
            resultado = alternativa

    if resultado.tipo == "criar":
        lem = lembretes.adicionar(
            chat_id,
            resultado.texto,
            resultado.rrule,
            resultado.primeira,
            origem=origem,
            transcricao=texto if origem == "audio" else None,
        )
        resposta = _confirma_criacao(lem)
        if _agendador:
            _agendador.acordar()
    elif resultado.tipo == "cancelar":
        resposta = _cancelar(chat_id, resultado.texto)
    else:
        resposta = resultado.mensagem or HELP

    tg.send(chat_id, resposta, reply_to=reply_to)


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------

def _formato_audio(voice: dict) -> str:
    mime = (voice.get("mime_type") or "").lower()
    if "mpeg" in mime or "mp3" in mime:
        return "mp3"
    if "mp4" in mime or "m4a" in mime:
        return "m4a"
    if "wav" in mime:
        return "wav"
    if "ogg" in mime or "opus" in mime:
        return "ogg"
    return "ogg"


def _processar_audio(chat_id: int, msg: dict, message_id: int) -> None:
    if not config.AUDIO_ENABLED:
        tg.send(chat_id, "Audio desativado. Manda por texto.", reply_to=message_id)
        return

    voice = msg.get("voice") or msg.get("audio") or {}
    duracao = int(voice.get("duration") or 0)
    tamanho = int(voice.get("file_size") or 0)
    if duracao and duracao > config.TRANSCRIBE_MAX_SEG:
        tg.send(
            chat_id,
            f"Audio muito longo ({duracao}s). Limite: {config.TRANSCRIBE_MAX_SEG}s.",
            reply_to=message_id,
        )
        return
    if tamanho and tamanho > config.AUDIO_MAX_MB * 1024 * 1024:
        tg.send(
            chat_id,
            f"Audio muito grande. Limite: {config.AUDIO_MAX_MB} MB.",
            reply_to=message_id,
        )
        return

    tg.typing(chat_id)
    try:
        dados, _ = tg.download_file(voice["file_id"])
        if tamanho == 0 and len(dados) > config.AUDIO_MAX_MB * 1024 * 1024:
            tg.send(chat_id, f"Audio muito grande. Limite: {config.AUDIO_MAX_MB} MB.", reply_to=message_id)
            return
        formato = "ogg" if msg.get("voice") else _formato_audio(voice)
        texto = transcricao.transcrever(dados, formato)
    except Exception as e:
        log.error("falha transcrevendo audio: %s", tg.redact(e))
        tg.send(
            chat_id,
            "Nao consegui ouvir o audio. Pode repetir ou mandar por texto?",
            reply_to=message_id,
        )
        return

    if not texto:
        tg.send(chat_id, "Nao entendi o audio. Pode repetir?", reply_to=message_id)
        return

    log.info("chat %s audio -> %d chars", chat_id, len(texto))
    tg.send(chat_id, f'🎧 Ouvi: "{texto}"', reply_to=message_id)
    _processar(chat_id, texto, reply_to=message_id, origem="audio")


# ---------------------------------------------------------------------------
# Workers / handlers
# ---------------------------------------------------------------------------

def _worker(fn, chat_id: int, *args) -> None:
    with _busy_lock:
        _busy.add(chat_id)
    try:
        fn(chat_id, *args)
    except Exception as e:
        log.error("erro no worker: %s", tg.redact(e))
        try:
            tg.send(chat_id, f"Tive um problema: {tg.redact(e)}")
        except Exception:
            pass
    finally:
        with _busy_lock:
            _busy.discard(chat_id)


def handle_message(msg: dict) -> None:
    chat_id = msg["chat"]["id"]
    user_id = msg.get("from", {}).get("id")
    texto = (msg.get("text") or "").strip()
    message_id = msg["message_id"]

    if user_id not in ALLOWED:
        tg.send(chat_id, "Acesso nao autorizado.", reply_to=message_id)
        return

    if texto in ("/start", "/help"):
        tg.send(chat_id, HELP, reply_to=message_id)
        return
    if texto == "/id":
        tg.send(chat_id, f"user id: {user_id}", reply_to=message_id)
        return
    if texto in ("/listar", "/lembretes"):
        tg.send(chat_id, _listar(chat_id), reply_to=message_id)
        return
    if texto == "/cancelar" or texto.startswith("/cancelar "):
        alvo = texto[len("/cancelar") :].strip()
        tg.send(chat_id, _cancelar(chat_id, alvo), reply_to=message_id)
        return
    if texto == "/apagar_inativos" or texto.startswith("/apagar_inativos "):
        arg = texto[len("/apagar_inativos") :].strip()
        tg.send(chat_id, _apagar_inativos(chat_id, arg), reply_to=message_id)
        return
    if texto.startswith("/"):
        tg.send(chat_id, "Comando nao reconhecido.\n\n" + HELP, reply_to=message_id)
        return

    if msg.get("voice") or msg.get("audio"):
        if _is_busy(chat_id):
            tg.send(chat_id, "Ainda estou processando a mensagem anterior.", reply_to=message_id)
            return
        threading.Thread(
            target=_worker,
            args=(_processar_audio, chat_id, msg, message_id),
            daemon=True,
        ).start()
        return

    if not texto:
        return
    if _is_busy(chat_id):
        tg.send(chat_id, "Ainda estou processando a mensagem anterior.", reply_to=message_id)
        return
    threading.Thread(
        target=_worker,
        args=(_processar, chat_id, texto, message_id),
        daemon=True,
    ).start()


def _is_busy(chat_id: int) -> bool:
    with _busy_lock:
        return chat_id in _busy


def stop(signum, frame) -> None:
    global _running
    _running = False
    if _agendador:
        _agendador.parar()
    log.info(f"sinal {signum}: encerrando apos o poll atual...")


_running = True


def main() -> None:
    global _agendador

    if not tg.TOKEN:
        log.info("ERRO: defina TELEGRAM_BOT_TOKEN (veja .secrets/agenda/telegram.env)")
        sys.exit(1)

    if not ALLOWED:
        log.info(
            "ERRO: defina TELEGRAM_ALLOWED_USER_IDS (sem a lista o bot nao atende ninguem)"
        )
        sys.exit(1)

    ativos = lembretes.ativos()
    log.info(
        "%s iniciado (agente=%s tz=%s audio=%s fallback=%s lembretes=%d)",
        runtime.bot_name(),
        config.AGENT,
        config.TIMEZONE,
        "on" if config.AUDIO_ENABLED else "off",
        "on" if config.LLM_FALLBACK else "off",
        len(ativos),
    )

    _agendador = Agendador()
    _agendador.start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    offset = 0
    while _running:
        try:
            resp = tg.api(
                "getUpdates",
                timeout=LONG_POLL,
                offset=offset,
                allowed_updates='["message"]',
            )
        except requests.RequestException as e:
            log.info(f"getUpdates falhou: {tg.redact(e)}")
            time.sleep(5)
            continue

        for update in resp.get("result", []):
            offset = update["update_id"] + 1
            msg = update.get("message")
            if not msg:
                continue
            try:
                handle_message(msg)
            except Exception as e:
                log.info(f"erro processando update: {tg.redact(repr(e))}")
                try:
                    tg.send(msg["chat"]["id"], f"Erro inesperado: {tg.redact(e)}")
                except Exception:
                    pass

    if _agendador:
        _agendador.parar()
    log.info("encerrado")


if __name__ == "__main__":
    main()