#!/usr/bin/env python3
"""Bot do analista: perguntas em texto/audio -> tools SQL -> texto e graficos.

Long-polling nativo, um worker por chat (uma pergunta por vez). O agente so
consome o artefato SQLite gerado offline por `analista.ingestao`; nao escreve
na base nem a importa.

Fluxo:
    texto  -> interpretacao.responder()  (LLM escolhe a tool / SQL)
    audio  -> common.transcricao -> mesmo caminho
    tool   -> analista.ferramentas -> Resultado -> texto (+ PNG sob pedido)
"""
import os
import re
import secrets
import signal
import sys
import threading
import time
import unicodedata

import requests

from analista import config, conversa, db, ferramentas, formato, graficos, interpretacao
from analista.resultado import Resultado
from common import llm, logsetup, transcricao
from common import telegram_api as tg

log = logsetup.get_logger("bot")

ALLOWED = {
    int(x)
    for x in re.split(r"[,\s]+", os.environ.get("TELEGRAM_ALLOWED_USER_IDS", ""))
    if x.strip().isdigit()
}
LONG_POLL = 25

_busy: set = set()
_busy_lock = threading.Lock()
_running = True

# Resultados recentes por token, para os botoes inline (memoria, com TTL).
_resultados: dict[str, tuple[Resultado, float]] = {}
_res_lock = threading.Lock()
_RES_TTL = 3600
_RES_MAX = 20

_PALAVRAS_GRAFICO = (
    "grafico", "plot", "visual", "evolucao", "evolucao mensal", "curva", "desenhe",
)


def build_help() -> str:
    return (
        f"Sou {llm.bot_name()}, seu analista de vendas. Pergunte em texto ou "
        "audio.\n\n"
        "Exemplos:\n"
        "  qual foi a receita de 2024?\n"
        "  top 5 vendedores em 2024\n"
        "  comparar 2023 com 2024\n"
        "  margem por equipe no primeiro trimestre de 2024\n"
        "  evolucao mensal da receita em 2023 (grafico)\n"
        "  e so da Equipe Vega?\n\n"
        "Comandos diretos (sem LLM):\n"
        "  /resumo <periodo>\n"
        "  /ranking <dimensao> <periodo> [top_n]\n"
        "  /serie <metrica> <periodo>\n"
        "  /tabela <dimensao> <periodo>\n"
        "  /base      - periodo, contagens e totais da base\n"
        "  /sql <select>  - ultimo recurso, com guardrails\n"
        "  /grafico <pergunta> - forca o grafico\n"
        "  /limpar    - apaga o contexto da conversa\n"
        "  /id        - mostra seu user id\n\n"
        "Periodos: 2024, marco de 2024, 1o trimestre de 2023, "
        "ultimos 30 dias, de 01/03 a 15/04, tudo."
    )


HELP = build_help()


# ---------------------------------------------------------------------------
# Formatacao da resposta
# ---------------------------------------------------------------------------

def _corpo(res: Resultado, limite: int = 20) -> str:
    if len(res.linhas) == 1 and len(res.colunas) >= 5:
        return formato.resumo_texto(res)
    if res.linhas and res.linhas[0].get("participacao") is not None:
        return formato.lista(res, limite=min(limite, 10))
    return formato.tabela(res, limite=limite)


def _cabecalho(res: Resultado) -> list[str]:
    meta = res.metadados
    linhas = [f"<b>{tg.escape(res.titulo)}</b>"]
    if meta.get("periodo"):
        linhas.append(f"Periodo: {tg.escape(meta['periodo'])}")
    filtros = meta.get("filtros") or {}
    if filtros:
        texto = ", ".join(f"{k}={v}" for k, v in filtros.items())
        linhas.append(f"Filtros: {tg.escape(texto)}")
    if meta.get("fora_da_base"):
        linhas.append(f"A base cobre {tg.escape(str(meta.get('base')))}")
    if meta.get("linhas") is not None and not res.linhas:
        linhas.append("Nenhuma linha retornada.")
    return linhas


def _mensagem(res: Resultado) -> str:
    """Monta a mensagem HTML cabendo no limite do Telegram (~4096)."""
    for limite in (20, 12, 6, 3):
        corpo = _corpo(res, limite)
        texto = "\n".join(_cabecalho(res)) + f"\n\n<pre>{tg.escape(corpo)}</pre>"
        if len(texto) <= 3900:
            return texto
    return "\n".join(_cabecalho(res)) + f"\n\n{tg.escape(_corpo(res, 3))}"


def _botoes(res: Resultado, token: str) -> dict:
    linha = []
    if res.grafico:
        linha.append({"text": "Grafico", "callback_data": f"an:g:{token}"})
    linha.append({"text": "Tabela", "callback_data": f"an:t:{token}"})
    linha.append({"text": "CSV", "callback_data": f"an:c:{token}"})
    return {"inline_keyboard": [linha]}


def _guardar(res: Resultado) -> str:
    token = secrets.token_hex(3)
    agora = time.time()
    with _res_lock:
        _resultados[token] = (res, agora)
        vencidos = [k for k, (_, quando) in _resultados.items() if agora - quando > _RES_TTL]
        for chave in vencidos:
            _resultados.pop(chave, None)
        if len(_resultados) > _RES_MAX:
            mais_antigo = min(_resultados, key=lambda k: _resultados[k][1])
            _resultados.pop(mais_antigo, None)
    return token


def _recuperar(token: str) -> Resultado | None:
    with _res_lock:
        item = _resultados.get(token)
    if not item:
        return None
    res, quando = item
    if time.time() - quando > _RES_TTL:
        return None
    return res


def _quer_grafico(texto: str) -> bool:
    limpo = _sem_acento(texto).lower()
    return any(palavra in limpo for palavra in _PALAVRAS_GRAFICO)


def _sem_acento(texto: str) -> str:
    decomposto = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in decomposto if not unicodedata.combining(c))


def _enviar_grafico(chat_id: int, res: Resultado) -> None:
    try:
        caminho = graficos.gerar(res)
    except graficos.ErroGrafico as e:
        tg.send(chat_id, f"Nao deu para desenhar: {e}")
        return
    except Exception as e:
        log.error("falha gerando grafico: %r", e)
        tg.send(chat_id, "Nao consegui gerar o grafico.")
        return
    tg.typing(chat_id, "upload_photo")
    try:
        tg.send_photo(chat_id, str(caminho), legenda=res.titulo[:1000])
    except Exception as e:
        log.error("falha enviando foto: %s", tg.redact(e))
        tg.send(chat_id, "Nao consegui enviar o grafico.")
    finally:
        caminho.unlink(missing_ok=True)


def _enviar_csv(chat_id: int, res: Resultado) -> None:
    import csv

    pasta = config.STATE_DIR / "export"
    pasta.mkdir(parents=True, exist_ok=True)
    nome = "".join(c if c.isalnum() else "-" for c in res.titulo.lower())[:40].strip("-")
    caminho = pasta / f"{nome or 'resultado'}-{int(time.time())}.csv"
    with caminho.open("w", encoding="utf-8-sig", newline="") as fh:
        escritor = csv.writer(fh, delimiter=";")
        escritor.writerow([rotulo for _, rotulo in res.colunas])
        for linha in res.linhas:
            escritor.writerow(
                [formato.fmt(res.formato.get(chave), linha.get(chave)) for chave, _ in res.colunas]
            )
    try:
        tg.send_document(chat_id, str(caminho), legenda=res.titulo[:1000])
    except Exception as e:
        log.error("falha enviando csv: %s", tg.redact(e))
        tg.send(chat_id, "Nao consegui gerar o CSV.")
    finally:
        caminho.unlink(missing_ok=True)


def _publicar(chat_id: int, res: Resultado, reply_to=None, com_grafico: bool = False) -> None:
    token = _guardar(res)
    tg.send(
        chat_id,
        _mensagem(res),
        reply_to=reply_to,
        botoes=_botoes(res, token),
        parse_mode="HTML",
    )
    if com_grafico and res.grafico:
        _enviar_grafico(chat_id, res)


# ---------------------------------------------------------------------------
# Comandos
# ---------------------------------------------------------------------------

def _parse_comando(texto: str) -> tuple[str, str]:
    if not texto.startswith("/"):
        return "", ""
    partes = texto.split(maxsplit=1)
    nome = partes[0].split("@", 1)[0].lower()
    return nome, (partes[1].strip() if len(partes) > 1 else "")


def _comando_direto(comando: str, argumentos: str) -> tuple[object, str]:
    """Devolve (Resultado|str, nome_da_tool) para os comandos sem LLM."""
    try:
        if comando == "/base":
            return ferramentas.info_base(), "info_base"

        if comando == "/resumo":
            return ferramentas.resumo(periodo=argumentos or None), "resumo"

        if comando == "/ranking":
            partes = argumentos.split()
            if not partes:
                return "Uso: /ranking <dimensao> [periodo] [top_n]", "ranking"
            dimensao = partes[0]
            resto = partes[1:]
            top_n = 10
            if resto and resto[-1].isdigit():
                top_n = int(resto.pop())
            periodo = " ".join(resto) or None
            return ferramentas.ranking(dimensao, periodo, top_n=top_n), "ranking"

        if comando == "/serie":
            partes = argumentos.split()
            metrica = "receita"
            if partes and partes[0].lower() in ferramentas.METRICAS:
                metrica = partes.pop(0)
            periodo = " ".join(partes) or None
            return ferramentas.serie(periodo, metrica=metrica), "serie"

        if comando == "/tabela":
            partes = argumentos.split()
            if not partes:
                return "Uso: /tabela <dimensao> [periodo]", "tabela"
            return ferramentas.tabela(partes[0], " ".join(partes[1:]) or None), "tabela"

        if comando == "/sql":
            if not argumentos:
                return "Uso: /sql select ... from v_vendas", "consultar_sql"
            return ferramentas.consultar_sql(argumentos), "consultar_sql"
    except ferramentas.ErroFerramenta as e:
        return f"Erro: {e}", comando.lstrip("/")
    return "Comando nao reconhecido.", comando


# ---------------------------------------------------------------------------
# Callbacks dos botoes
# ---------------------------------------------------------------------------

def handle_callback(cb: dict) -> None:
    cb_id = cb.get("id")
    user_id = (cb.get("from") or {}).get("id")
    mensagem = cb.get("message") or {}
    chat_id = (mensagem.get("chat") or {}).get("id")
    if not cb_id or chat_id is None:
        return
    if user_id not in ALLOWED:
        tg.responder_callback(cb_id, "Acesso nao autorizado.")
        return

    partes = (cb.get("data") or "").split(":")
    if len(partes) < 3 or partes[0] != "an":
        tg.responder_callback(cb_id)
        return

    acao, token = partes[1], partes[2]
    res = _recuperar(token)
    if res is None:
        tg.responder_callback(cb_id, "Este resultado expirou. Refaça a pergunta.")
        return
    tg.responder_callback(cb_id)

    if acao == "g":
        _enviar_grafico(chat_id, res)
    elif acao == "c":
        _enviar_csv(chat_id, res)
    elif acao == "t":
        tg.send(chat_id, _mensagem(res), parse_mode="HTML")


# ---------------------------------------------------------------------------
# Processamento
# ---------------------------------------------------------------------------

def _checar_base(chat_id: int, reply_to=None) -> bool:
    ok, motivo = db.status()
    if not ok:
        log.error("base indisponivel: %s", motivo)
        tg.send(chat_id, f"Base indisponivel.\n{motivo}", reply_to=reply_to)
    return ok


def _processar(chat_id: int, texto: str, reply_to=None, origem: str = "texto") -> None:
    if not _checar_base(chat_id, reply_to):
        return

    comando, argumentos = _parse_comando(texto)
    if comando in ("/base", "/resumo", "/ranking", "/serie", "/tabela", "/sql"):
        resultado, nome = _comando_direto(comando, argumentos)
        if isinstance(resultado, Resultado):
            conversa.registrar(chat_id, texto, nome, {})
            _publicar(chat_id, resultado, reply_to=reply_to)
        else:
            tg.send(chat_id, resultado, reply_to=reply_to)
        return

    tg.typing(chat_id)
    resposta = interpretacao.responder(texto, chat_id)
    if resposta.texto:
        tg.send(chat_id, resposta.texto, reply_to=reply_to)
        return
    if resposta.resultado is not None:
        _publicar(
            chat_id,
            resposta.resultado,
            reply_to=reply_to,
            com_grafico=_quer_grafico(texto),
        )


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
    return "ogg"


def _processar_audio(chat_id: int, msg: dict, message_id: int) -> None:
    if not config.AUDIO_ENABLED:
        tg.send(chat_id, "Audio desativado. Manda por texto.", reply_to=message_id)
        return

    voice = msg.get("voice") or msg.get("audio") or {}
    duracao = int(voice.get("duration") or 0)
    if duracao and duracao > config.TRANSCRIBE_MAX_SEG:
        tg.send(
            chat_id,
            f"Audio muito longo ({duracao}s). Limite: {config.TRANSCRIBE_MAX_SEG}s.",
            reply_to=message_id,
        )
        return

    tg.typing(chat_id)
    try:
        dados, _ = tg.download_file(voice["file_id"])
        if len(dados) > config.AUDIO_MAX_MB * 1024 * 1024:
            tg.send(chat_id, f"Audio muito grande. Limite: {config.AUDIO_MAX_MB} MB.", reply_to=message_id)
            return
        formato_audio = "ogg" if msg.get("voice") else _formato_audio(voice)
        texto = transcricao.transcrever(dados, formato_audio)
    except Exception as e:
        log.error("falha transcrevendo audio: %s", tg.redact(e))
        tg.send(chat_id, "Nao consegui ouvir o audio. Pode repetir ou mandar por texto?", reply_to=message_id)
        return

    if not texto:
        tg.send(chat_id, "Nao entendi o audio. Pode repetir?", reply_to=message_id)
        return

    tg.send(chat_id, f'Ouvi: "{texto}"', reply_to=message_id)
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


def _is_busy(chat_id: int) -> bool:
    with _busy_lock:
        return chat_id in _busy


def handle_message(msg: dict) -> None:
    chat_id = msg["chat"]["id"]
    user_id = msg.get("from", {}).get("id")
    texto = (msg.get("text") or "").strip()
    message_id = msg["message_id"]

    if user_id not in ALLOWED:
        tg.send(chat_id, "Acesso nao autorizado.", reply_to=message_id)
        return

    comando, argumentos = _parse_comando(texto)

    if comando in ("/start", "/help"):
        tg.send(chat_id, HELP, reply_to=message_id)
        return
    if comando == "/id":
        tg.send(chat_id, f"user id: {user_id}", reply_to=message_id)
        return
    if comando == "/limpar":
        conversa.limpar(chat_id)
        tg.send(chat_id, "Contexto apagado.", reply_to=message_id)
        return
    if comando == "/grafico":
        texto = argumentos
        comando = ""
        if not texto:
            tg.send(chat_id, "Uso: /grafico <pergunta>", reply_to=message_id)
            return

    if comando and comando not in ("/base", "/resumo", "/ranking", "/serie", "/tabela", "/sql"):
        tg.send(chat_id, "Comando nao reconhecido.\n\n" + HELP, reply_to=message_id)
        return

    if msg.get("voice") or msg.get("audio"):
        if _is_busy(chat_id):
            tg.send(chat_id, "Ainda estou processando a mensagem anterior.", reply_to=message_id)
            return
        threading.Thread(
            target=_worker, args=(_processar_audio, chat_id, msg, message_id), daemon=True
        ).start()
        return

    if not texto:
        return
    if _is_busy(chat_id):
        tg.send(chat_id, "Ainda estou processando a mensagem anterior.", reply_to=message_id)
        return
    threading.Thread(
        target=_worker, args=(_processar, chat_id, texto, message_id), daemon=True
    ).start()


def stop(signum, frame) -> None:
    global _running
    _running = False
    log.info("sinal %s: encerrando apos o poll atual...", signum)


def main() -> None:
    if not tg.token():
        log.info("ERRO: defina TELEGRAM_BOT_TOKEN (veja .secrets/analista/telegram.env)")
        sys.exit(1)
    if not ALLOWED:
        log.info("ERRO: defina TELEGRAM_ALLOWED_USER_IDS (sem a lista o bot nao atende ninguem)")
        sys.exit(1)

    ok, motivo = db.status()
    log.info(
        "%s iniciado (agente=%s tz=%s base=%s graficos=%s)",
        llm.bot_name(),
        config.AGENT,
        config.TIMEZONE,
        "ok" if ok else f"INDISPONIVEL ({motivo})",
        "on" if config.GRAFICO_ENABLED else "off",
    )

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    offset = 0
    while _running:
        try:
            resp = tg.api(
                "getUpdates",
                timeout=LONG_POLL,
                offset=offset,
                allowed_updates='["message","callback_query"]',
            )
        except requests.RequestException as e:
            log.info("getUpdates falhou: %s", tg.redact(e))
            time.sleep(5)
            continue

        for update in resp.get("result", []):
            offset = update["update_id"] + 1
            cb = update.get("callback_query")
            if cb:
                try:
                    handle_callback(cb)
                except Exception as e:
                    log.info("erro processando callback: %s", tg.redact(repr(e)))
                continue
            msg = update.get("message")
            if not msg:
                continue
            try:
                handle_message(msg)
            except Exception as e:
                log.info("erro processando update: %s", tg.redact(repr(e)))
                try:
                    tg.send(msg["chat"]["id"], f"Erro inesperado: {tg.redact(e)}")
                except Exception:
                    pass

    log.info("encerrado")


if __name__ == "__main__":
    main()