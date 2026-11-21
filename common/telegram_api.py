#!/usr/bin/env python3
"""Cliente minimo da Bot API do Telegram (long-polling, envio e download).

Centraliza o token para que todo log/erro seja redigido. O token e lido do
ambiente a cada chamada (nao no import), entao nao ha dependencia de ordem
entre este modulo e o `config` que carrega os `.secrets`.

Usado por `<agente>/bot.py` (polling), pelo agendador (notificacoes) e pelo
analista (graficos e exportacoes).
"""
import json
import os

import requests


def token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()


def _api() -> str:
    return f"https://api.telegram.org/bot{token()}"


def _file_api() -> str:
    return f"https://api.telegram.org/file/bot{token()}"


def redact(msg) -> str:
    """Remove o token do bot de qualquer mensagem/erro antes de logar.

    Excecoes do requests incluem a URL completa, que contem o token em
    https://api.telegram.org/bot<TOKEN>/<method>.
    """
    text = str(msg)
    tok = token()
    return text.replace(tok, "***") if tok else text


def _post(method: str, files=None, **params):
    resp = requests.post(f"{_api()}/{method}", data=params, files=files, timeout=120)
    resp.raise_for_status()
    return resp.json()


def api(method: str, **params):
    return _post(method, **params)


def send(
    chat_id,
    text: str,
    reply_to=None,
    botoes=None,
    parse_mode: str | None = None,
) -> None:
    """Envia texto, quebrando em blocos de 4000 chars.

    `parse_mode="HTML"` deixa o chamador usar `<pre>`/`<b>`; nesse caso ele e
    responsavel por escapar o conteudo dinamico (use `escape`).
    """
    primeiro = True
    for chunk in _chunks(text):
        params = {
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            params["parse_mode"] = parse_mode
        if reply_to is not None:
            params["reply_to_message_id"] = reply_to
            reply_to = None
        if primeiro and botoes:
            params["reply_markup"] = json.dumps(botoes)
        primeiro = False
        api("sendMessage", **params)


def send_photo(chat_id, caminho: str, legenda: str | None = None, reply_to=None, botoes=None) -> None:
    """Envia um PNG/JPEG (limite de 1024 chars na legenda: mande o texto a parte)."""
    params = {"chat_id": chat_id}
    if legenda:
        params["caption"] = legenda[:1024]
    if reply_to is not None:
        params["reply_to_message_id"] = reply_to
    if botoes:
        params["reply_markup"] = json.dumps(botoes)
    nome = os.path.basename(caminho)
    with open(caminho, "rb") as fh:
        _post("sendPhoto", files={"photo": (nome, fh, "image/png")}, **params)


def send_document(chat_id, caminho: str, legenda: str | None = None, reply_to=None) -> None:
    """Envia um arquivo (CSV, XLSX) como documento."""
    params = {"chat_id": chat_id}
    if legenda:
        params["caption"] = legenda[:1024]
    if reply_to is not None:
        params["reply_to_message_id"] = reply_to
    nome = os.path.basename(caminho)
    with open(caminho, "rb") as fh:
        _post(
            "sendDocument",
            files={"document": (nome, fh, "application/octet-stream")},
            **params,
        )


def responder_callback(callback_id: str, texto: str = "") -> None:
    """Fecha o "carregando" do botao inline (senao o Telegram fica girando)."""
    try:
        params = {"callback_query_id": callback_id}
        if texto:
            params["text"] = texto
        api("answerCallbackQuery", **params)
    except Exception:
        # callback antigo expira; nao vale derrubar o polling por isso
        pass


def editar(chat_id, message_id, text: str, botoes=None, parse_mode: str | None = None) -> None:
    """Reescreve uma mensagem do bot. Sem `botoes`, remove o teclado inline."""
    params = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "disable_web_page_preview": True,
        "reply_markup": json.dumps(botoes or {"inline_keyboard": []}),
    }
    if parse_mode:
        params["parse_mode"] = parse_mode
    api("editMessageText", **params)


def typing(chat_id, action: str = "typing") -> None:
    try:
        api("sendChatAction", chat_id=chat_id, action=action)
    except Exception:
        pass


def download_file(file_id: str):
    """Baixa um arquivo do Telegram e devolve (bytes, file_path)."""
    data = api("getFile", file_id=file_id)
    file_path = data["result"]["file_path"]
    resp = requests.get(f"{_file_api()}/{file_path}", timeout=120)
    resp.raise_for_status()
    return resp.content, file_path


def escape(texto: str) -> str:
    """Escapa texto para `parse_mode="HTML"`."""
    return (
        str(texto)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _chunks(text, limit: int = 4000):
    text = text or ""
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) > limit and cur:
            chunks.append(cur)
            cur = ""
        cur += line
    if cur:
        chunks.append(cur)
    return chunks