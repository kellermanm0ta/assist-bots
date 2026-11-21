#!/usr/bin/env python3
"""CLI das tools do analista: testa a base e as consultas sem Telegram e sem LLM.

Exemplos:
    python -m analista.consultas info_base
    python -m analista.consultas resumo periodo=2024
    python -m analista.consultas ranking dimensao=vendedor periodo=2024 top_n=5
    python -m analista.consultas serie metrica=receita granularidade=mensal periodo=2023
    python -m analista.consultas comparar periodo_a=2023 periodo_b=2024
    python -m analista.consultas distribuicao dimensao=categoria
    python -m analista.consultas consultar_sql sql="select equipe, sum(receita) v from v_vendas group by equipe"

Os argumentos sao os mesmos do contrato JSON (`{"ferramenta": ..., "args": ...}`).
"""
from __future__ import annotations

import argparse
import json
import sys

from analista import db, ferramentas, formato


def _valor(txt: str):
    """Tenta ler como JSON (int, float, lista, dict, true/false); senao texto."""
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        return txt


def _parse_args(pares: list[str]) -> dict:
    args: dict = {}
    for par in pares:
        if "=" not in par:
            raise SystemExit(f"argumento invalido: {par!r} (use chave=valor)")
        chave, valor = par.split("=", 1)
        args[chave.strip()] = _valor(valor.strip())
    return args


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m analista.consultas",
        description="Executa uma tool do analista direto na base.",
    )
    parser.add_argument("ferramenta", help=f"uma de: {', '.join(sorted(ferramentas.CATALOGO))}")
    parser.add_argument("args", nargs="*", help="chave=valor (ex: periodo=2024)")
    parser.add_argument("--json", action="store_true", dest="como_json",
                        help="imprime o resultado cru em JSON")
    parser.add_argument("--limite", type=int, default=25, help="linhas na tabela")
    parser.add_argument("--grafico", nargs="?", const="", default=None, metavar="PNG",
                        help="gera o PNG do resultado (opcional: caminho de saida)")
    opcoes = parser.parse_args(argv)

    ok, mensagem = db.status()
    if not ok:
        print(mensagem, file=sys.stderr)
        return 1

    try:
        res = ferramentas.executar(opcoes.ferramenta, _parse_args(opcoes.args))
    except ferramentas.ErroFerramenta as e:
        print(f"erro: {e}", file=sys.stderr)
        return 2

    if opcoes.como_json:
        print(json.dumps(res.para_dict(), ensure_ascii=False, indent=2))
        return 0

    if opcoes.grafico is not None:
        from pathlib import Path

        from analista import graficos

        try:
            png = graficos.gerar(res, Path(opcoes.grafico) if opcoes.grafico else None)
        except graficos.ErroGrafico as e:
            print(f"grafico: {e}", file=sys.stderr)
        else:
            print(f"png: {png}")

    print(res.titulo)
    if "periodo" in res.metadados:
        print(f"periodo: {res.metadados['periodo']}")
    filtros = res.metadados.get("filtros") or {}
    if filtros:
        print("filtros: " + ", ".join(f"{k}={v}" for k, v in filtros.items()))
    if res.metadados.get("fora_da_base"):
        print(f"ATENCAO: o periodo nao cruza a base ({res.metadados.get('base')})")
    if res.metadados.get("sql"):
        print("sql: " + " ".join(res.metadados["sql"].split()))
    print()
    if res.titulo.startswith("Resumo"):
        print(formato.resumo_texto(res))
    elif res.linhas and "participacao" in (res.linhas[0] or {}):
        print(formato.lista(res, limite=opcoes.limite))
    else:
        print(formato.tabela(res, limite=opcoes.limite))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())