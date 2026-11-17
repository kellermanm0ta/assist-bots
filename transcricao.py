#!/usr/bin/env python3
"""Transcricao de audio via OpenRouter (modelo multimodal).

O Telegram entrega notas de voz em OGG/Opus. O Chat Completions da OpenRouter
aceita `input_audio` em base64 com o formato `ogg`; o modelo (Gemini) devolve
o texto. Nenhuma dependencia local de ffmpeg/whisper.
"""
from __future__ import annotations

import base64
import os
import sys

import requests

import config
import logsetup

log = logsetup.get_logger("transcricao")

API = "https://openrouter.ai/api/v1/chat/completions"

_PROMPT = (
    "Transcreva literalmente o audio em portugues do Brasil. "
    "Devolva apenas a transcricao, sem comentarios, sem rotulos e sem traducao."
)


def transcrever(dados: bytes, formato: str = "ogg") -> str:
    chave = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not chave:
        raise RuntimeError("OPENROUTER_API_KEY nao definida (veja .secrets/openrouter.env)")

    payload = {
        "model": config.TRANSCRIBE_MODEL,
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _PROMPT},
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": base64.b64encode(dados).decode("ascii"),
                            "format": formato or "ogg",
                        },
                    },
                ],
            }
        ],
    }
    resp = requests.post(
        API,
        headers={"Authorization": f"Bearer {chave}", "Content-Type": "application/json"},
        json=payload,
        timeout=120,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"OpenRouter HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    texto = (data["choices"][0]["message"]["content"] or "").strip()
    log.info("audio transcrito (%d bytes, %s) -> %d chars", len(dados), formato, len(texto))
    return texto


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("uso: python transcricao.py <arquivo-de-audio>", file=sys.stderr)
        sys.exit(2)
    caminho = sys.argv[1]
    fmt = caminho.rsplit(".", 1)[-1].lower() if "." in caminho else "ogg"
    with open(caminho, "rb") as fh:
        print(transcrever(fh.read(), fmt))