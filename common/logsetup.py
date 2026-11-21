#!/usr/bin/env python3
"""Configuracao de logging: tudo vai para stdout/stderr (12-factor).

No Docker aparece em `docker logs`; no host o `run_bot.sh` redireciona para
`STATE_DIR/bot.log`. Nao ha gravacao de arquivo dentro da aplicacao.

O nome do logger vem de `AGENT` no ambiente, que `common.config.iniciar()`
normaliza. Nao importamos `config` aqui de proposito: assim o log funciona
mesmo se este modulo for importado antes do config do agente.
"""
import logging
import os
import sys

_configurados: set[str] = set()


def _raiz() -> str:
    return os.environ.get("BOT_AGENT", "").strip() or "assist"


def setup_logging(level: int = logging.INFO) -> None:
    raiz = _raiz()
    if raiz in _configurados:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root = logging.getLogger(raiz)
    root.setLevel(level)
    root.addHandler(handler)
    root.propagate = False
    _configurados.add(raiz)


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(f"{_raiz()}.{name}")