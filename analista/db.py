#!/usr/bin/env python3
"""Acesso ao artefato SQLite gerado por `analista.ingestao`.

Sempre read-only e sempre com guardrails: o bot nunca escreve na base e o LLM
nunca executa SQL arbitrario sem passar por `validar_sql()`. As ferramentas do
pacote usam `consultar()` (SQL nosso, parametrizado); so o escape hatch usa
`executar_sql()`, que valida antes de tocar no banco.
"""
from __future__ import annotations

import re
import sqlite3
import time
from urllib.parse import quote

from analista import config
from common import logsetup

log = logsetup.get_logger("db")

SCHEMA_SUPORTADO = 1

# Tudo que o SQL livre pode citar em FROM/JOIN.
PERMITIDAS = {"vendas", "v_vendas", "v_mensal", "meta"}

_PROIBIDO = re.compile(
    r"\b(attach|detach|pragma|insert|update|delete|drop|alter|create|replace|"
    r"vacuum|reindex|analyze|load_extension|savepoint|begin|commit|rollback|"
    r"truncate|grant|revoke)\b",
    re.IGNORECASE,
)
_TABELAS = re.compile(r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
_COMENTARIO = re.compile(r"(--[^\n]*|/\*.*?\*/)", re.DOTALL)


class ErroBase(RuntimeError):
    """Artefato ausente, ilegivel ou em versao de schema incompativel."""


class ErroSQL(ValueError):
    """SQL livre recusado pelos guardrails."""


def status() -> tuple[bool, str]:
    """(ok, mensagem) para o boot e para os comandos de diagnostico."""
    db = config.ANALISTA_DB
    if not db.exists():
        return False, (
            f"base ausente em {db}. Gere com "
            "`python -m analista.ingestao --csv <dir|arquivo>`"
        )
    try:
        con = conectar()
    except sqlite3.Error as e:
        return False, f"nao consegui abrir {db}: {e}"
    try:
        linha = con.execute(
            "SELECT valor FROM meta WHERE chave = 'schema_version'"
        ).fetchone()
    except sqlite3.Error as e:
        return False, f"artefato sem tabela meta: {e}"
    finally:
        con.close()
    versao = (linha[0] if linha else "") or ""
    if versao != str(SCHEMA_SUPORTADO):
        return False, (
            f"schema_version {versao or '?'} incompativel "
            f"(esperado {SCHEMA_SUPORTADO}); regenere com --forcar"
        )
    return True, "ok"


def conectar() -> sqlite3.Connection:
    db = config.ANALISTA_DB
    if not db.exists():
        raise ErroBase(f"base ausente em {db}")
    uri = f"file:{quote(str(db), safe='/')}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only = ON")
    # Aborta consulta que passe do tempo limite (o handler roda a cada N ops).
    inicio = time.monotonic()

    def _guarda() -> int:
        return 1 if time.monotonic() - inicio > config.TIMEOUT_SQL else 0

    con.set_progress_handler(_guarda, 20000)
    return con


def _resultado(cur: sqlite3.Cursor) -> tuple[list[str], list[dict]]:
    colunas = [d[0] for d in (cur.description or [])]
    return colunas, [dict(linha) for linha in cur.fetchall()]


class ErroConsulta(RuntimeError):
    """Falha de execucao numa consulta interna."""


def consultar(sql: str, params: tuple | list = ()) -> tuple[list[str], list[dict]]:
    """SQL construido por nos: ainda limita linhas e propaga ErroBase."""
    con = conectar()
    try:
        cur = con.execute(sql, tuple(params))
        return _resultado(cur)
    except sqlite3.Error as e:
        raise ErroConsulta(f"{e}") from e
    finally:
        con.close()


def validar_sql(sql: str) -> str:
    """Devolve o SQL normalizado (sem `;`) ou levanta ErroSQL."""
    texto = _COMENTARIO.sub(" ", sql or "").strip()
    if not texto:
        raise ErroSQL("SQL vazio")
    texto = texto.rstrip(";").strip()
    if not re.match(r"^(select|with)\b", texto, re.IGNORECASE):
        raise ErroSQL("apenas SELECT (ou WITH ... SELECT) e permitido")
    if ";" in texto:
        raise ErroSQL("apenas uma instrucao por consulta")
    proibido = _PROIBIDO.search(texto)
    if proibido:
        raise ErroSQL(f"comando proibido: {proibido.group(1).upper()}")
    for nome in _TABELAS.findall(texto):
        if nome.lower() not in PERMITIDAS:
            raise ErroSQL(
                f"tabela fora da whitelist: {nome} "
                f"(use {', '.join(sorted(PERMITIDAS))})"
            )
    return texto


def executar_sql(sql: str, limite: int | None = None) -> tuple[list[str], list[dict], str]:
    """Escape hatch do LLM: valida, envolve em LIMIT e executa."""
    texto = validar_sql(sql)
    teto = limite or config.MAX_LINHAS
    final = f"SELECT * FROM (\n{texto}\n) LIMIT {int(teto)}"
    colunas, linhas = consultar(final)
    log.info("sql livre (%d linhas): %s", len(linhas), " ".join(texto.split())[:300])
    return colunas, linhas, final