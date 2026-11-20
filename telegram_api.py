#!/usr/bin/env python3
"""Cliente minimo da Bot API do Telegram (long-polling e envio).

Centraliza o token para que todo log/erro seja redigido. Usado pelo
`telegram_bot.py` (polling) e pelo `agendador.py` (notificacoes).
"""
import json
import os

import requests

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
API = f"https://api.telegram.org/bot{TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{TOKEN}"


def redact(msg) -> str:
    """Remove o token do bot de qualquer mensagem/erro antes de logar.

    Excecoes do requests incluem a URL completa, que contem o token em
    https://api.telegram.org/bot<TOKEN>/<method>.
    """
    text = str(msg)
    return text.replace(TOKEN, "***") if TOKEN else text


def api(method: str, **params):
    resp = requests.post(f"{API}/{method}", data=params, timeout=90)
    resp.raise_for_status()
    return resp.json()


def send(chat_id, text: str, reply_to=None, botoes=None) -> None:
    primeiro = True
    for chunk in _chunks(text):
        params = {
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }
        if reply_to is not None:
            params["reply_to_message_id"] = reply_to
            reply_to = None
        if primeiro and botoes:
            params["reply_markup"] = json.dumps(botoes)
        primeiro = False
        api("sendMessage", **params)


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


def editar(chat_id, message_id, text: str, botoes=None) -> None:
    """Reescreve uma mensagem do bot. Sem `botoes`, remove o teclado inline."""
    api(
        "editMessageText",
        chat_id=chat_id,
        message_id=message_id,
        text=text,
        disable_web_page_preview=True,
        reply_markup=json.dumps(botoes or {"inline_keyboard": []}),
    )


def typing(chat_id, action: str = "typing") -> None:
    try:
        api("sendChatAction", chat_id=chat_id, action=action)
    except Exception:
        pass


def download_file(file_id: str):
    """Baixa um arquivo do Telegram e devolve (bytes, file_path)."""
    data = api("getFile", file_id=file_id)
    file_path = data["result"]["file_path"]
    resp = requests.get(f"{FILE_API}/{file_path}", timeout=120)
    resp.raise_for_status()
    return resp.content, file_path


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