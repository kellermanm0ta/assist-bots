#!/usr/bin/env python3
"""As tools do analista: cada uma vira uma consulta SQL parametrizada.

Regra de ouro: numero so sai daqui vindo do SQLite. Nenhuma tool calcula
agregado em Python (so formatacao/participacao derivada de um total tambem
consultado). Isso mantem a resposta auditavel e o LLM longe da aritmetica.

Toda funcao aceita o periodo como texto pt-BR ("2024", "marco de 2023",
"ultimos 30 dias") e resolve via `analista.periodo`.
"""
from __future__ import annotations

import json
import re
from datetime import date

from analista import config, db, periodo
from analista.resultado import METRICAS_ADITIVAS, Resultado
from common import logsetup

log = logsetup.get_logger("ferramentas")

# dimensao -> (expressao SQL, rotulo)
DIMENSOES: dict[str, tuple[str, str]] = {
    "equipe": ("equipe", "Equipe"),
    "vendedor": ("vendedor", "Vendedor"),
    "servico": ("servico", "Servico"),
    "categoria": ("categoria", "Categoria"),
    "cliente": ("cliente", "Cliente"),
    "estado": ("estado", "Estado"),
    "regiao": ("regiao", "Regiao"),
    "mes": ("mes", "Mes"),
    "ano": ("ano", "Ano"),
    "trimestre": ("ano || '-T' || trimestre", "Trimestre"),
    "dia_semana": ("dia_semana", "Dia da semana"),
}

# metrica -> (expressao SQL, rotulo, formato)
METRICAS: dict[str, tuple[str, str, str]] = {
    "receita": ("SUM(receita)", "Receita", "brl"),
    "custo": ("SUM(custo_total)", "Custo", "brl"),
    "margem": ("SUM(margem)", "Margem", "brl"),
    "margem_pct": (
        "100.0 * SUM(margem) / NULLIF(SUM(receita), 0)",
        "Margem %",
        "pct",
    ),
    "vendas": ("COUNT(*)", "Vendas", "num"),
    "quantidade": ("SUM(quantidade)", "Quantidade", "num"),
    "ticket": (
        "SUM(receita) / NULLIF(COUNT(*), 0)",
        "Ticket medio",
        "brl",
    ),
}

# granularidade -> (expressao de agrupamento, rotulo)
GRANULARIDADES: dict[str, tuple[str, str]] = {
    "dia": ("dia", "Dia"),
    "diaria": ("dia", "Dia"),
    "diario": ("dia", "Dia"),
    "semana": ("strftime('%Y-%W', data)", "Semana"),
    "semanal": ("strftime('%Y-%W', data)", "Semana"),
    "mes": ("mes", "Mes"),
    "mensal": ("mes", "Mes"),
    "trimestre": ("ano || '-T' || trimestre", "Trimestre"),
    "trimestral": ("ano || '-T' || trimestre", "Trimestre"),
    "ano": ("ano", "Ano"),
    "anual": ("ano", "Ano"),
}

DIAS = ["Domingo", "Segunda", "Terca", "Quarta", "Quinta", "Sexta", "Sabado"]


class ErroFerramenta(ValueError):
    """Argumento invalido ou periodo nao entendido."""


# ---------------------------------------------------------------------------
# Infra
# ---------------------------------------------------------------------------

_base_cache: tuple[float, tuple[date, date] | None] | None = None


def base() -> tuple[date, date] | None:
    """(data_min, data_max) do artefato, recalculado se o arquivo mudar."""
    global _base_cache
    db_path = config.ANALISTA_DB
    try:
        marca = db_path.stat().st_mtime
    except OSError:
        return None
    if _base_cache and _base_cache[0] == marca:
        return _base_cache[1]

    _, linhas = db.consultar(
        "SELECT chave, valor FROM meta WHERE chave IN ('data_min', 'data_max')"
    )
    meta = {linha["chave"]: linha["valor"] for linha in linhas}
    try:
        intervalo = (
            date.fromisoformat(meta["data_min"]),
            date.fromisoformat(meta["data_max"]),
        )
    except (KeyError, ValueError):
        intervalo = None
    _base_cache = (marca, intervalo)
    return intervalo


def _periodo(texto: str | None) -> periodo.Periodo:
    try:
        return periodo.resolver(texto, base=base(), ref=config.agora().date())
    except periodo.ErroPeriodo as e:
        raise ErroFerramenta(str(e)) from e


def _dimensao(nome: str) -> tuple[str, str]:
    chave = (nome or "").strip().lower()
    if chave not in DIMENSOES:
        raise ErroFerramenta(
            f'dimensao "{nome}" invalida. Use: {", ".join(sorted(DIMENSOES))}'
        )
    return DIMENSOES[chave]


def _metrica(nome: str) -> tuple[str, str, str]:
    chave = (nome or "receita").strip().lower()
    if chave not in METRICAS:
        raise ErroFerramenta(
            f'metrica "{nome}" invalida. Use: {", ".join(sorted(METRICAS))}'
        )
    return METRICAS[chave]


def _where(p: periodo.Periodo, filtros: dict | None) -> tuple[str, list]:
    condicoes = ["data BETWEEN ? AND ?"]
    params: list = [p.inicio, p.fim]
    for chave, valor in (filtros or {}).items():
        expressao, _ = _dimensao(chave)
        valores = valor if isinstance(valor, (list, tuple, set)) else [valor]
        valores = [str(v) for v in valores if v is not None and str(v) != ""]
        if not valores:
            continue
        if len(valores) == 1:
            condicoes.append(f"{expressao} = ?")
        else:
            condicoes.append(f"{expressao} IN ({','.join('?' * len(valores))})")
        params.extend(valores)
    return " AND ".join(condicoes), params


def _rotular_dias(linhas: list[dict]) -> list[dict]:
    for linha in linhas:
        try:
            linha["rotulo"] = DIAS[int(linha["rotulo"]) % 7]
        except (KeyError, ValueError, TypeError):
            pass
    return linhas


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def info_base() -> Resultado:
    """O que existe no artefato: periodo, contagens e totais."""
    from analista import formato  # evita ciclo no import do modulo

    _, linhas = db.consultar("SELECT chave, valor FROM meta")
    meta = {linha["chave"]: linha["valor"] for linha in linhas}

    _, dados = db.consultar(
        "SELECT COUNT(*) AS linhas, COUNT(DISTINCT equipe) AS equipes, "
        "COUNT(DISTINCT vendedor) AS vendedores, COUNT(DISTINCT servico) AS servicos, "
        "COUNT(DISTINCT cliente) AS clientes FROM vendas"
    )
    d = dados[0] if dados else {}

    itens = [
        ("Linhas", formato.numero(d.get("linhas"))),
        ("Periodo", f"{meta.get('data_min')} a {meta.get('data_max')}"),
        ("Equipes", formato.numero(d.get("equipes"))),
        ("Vendedores", formato.numero(d.get("vendedores"))),
        ("Servicos", formato.numero(d.get("servicos"))),
        ("Clientes", formato.numero(d.get("clientes"))),
        ("Receita", formato.brl(float(meta.get("receita_total") or 0))),
        ("Custo", formato.brl(float(meta.get("custo_total") or 0))),
        ("Margem", formato.brl(float(meta.get("margem_total") or 0))),
        ("Gerado em", meta.get("gerado_em") or "-"),
    ]
    return Resultado(
        titulo="Base de dados",
        colunas=[("rotulo", "Item"), ("valor", "Valor")],
        linhas=[{"rotulo": rotulo, "valor": valor} for rotulo, valor in itens],
        metadados={"schema_version": meta.get("schema_version")},
    )


def resumo(periodo: str | None = None, filtros: dict | None = None) -> Resultado:
    p = _periodo(periodo)
    onde, params = _where(p, filtros)
    _, linhas = db.consultar(
        "SELECT SUM(receita) AS receita, SUM(custo_total) AS custo, "
        "SUM(margem) AS margem, "
        "100.0 * SUM(margem) / NULLIF(SUM(receita), 0) AS margem_pct, "
        "COUNT(*) AS vendas, SUM(quantidade) AS quantidade, "
        "SUM(receita) / NULLIF(COUNT(*), 0) AS ticket "
        f"FROM v_vendas WHERE {onde}",
        params,
    )
    linha = linhas[0] if linhas else {}
    return Resultado(
        titulo=f"Resumo de {p.rotulo}",
        colunas=[
            ("receita", "Receita"),
            ("custo", "Custo"),
            ("margem", "Margem"),
            ("margem_pct", "Margem %"),
            ("vendas", "Vendas"),
            ("quantidade", "Quantidade"),
            ("ticket", "Ticket medio"),
        ],
        linhas=[linha],
        formato={
            "receita": "brl",
            "custo": "brl",
            "margem": "brl",
            "margem_pct": "pct",
            "vendas": "num",
            "quantidade": "num",
            "ticket": "brl",
        },
        metadados={
            "periodo": p.rotulo,
            "de": p.inicio,
            "ate": p.fim,
            "filtros": filtros or {},
        },
    )


def serie(
    periodo: str | None = None,
    granularidade: str = "mensal",
    metrica: str = "receita",
    filtros: dict | None = None,
) -> Resultado:
    p = _periodo(periodo)
    chave_gran = (granularidade or "mensal").strip().lower()
    if chave_gran not in GRANULARIDADES:
        raise ErroFerramenta(
            f'granularidade "{granularidade}" invalida. '
            f'Use: {", ".join(sorted(GRANULARIDADES))}'
        )
    expr_gran, rotulo_gran = GRANULARIDADES[chave_gran]
    expr_met, rotulo_met, tipo = _metrica(metrica)
    onde, params = _where(p, filtros)

    _, linhas = db.consultar(
        f"SELECT {expr_gran} AS rotulo, {expr_met} AS valor "
        f"FROM v_vendas WHERE {onde} GROUP BY {expr_gran} ORDER BY {expr_gran}",
        params,
    )
    return Resultado(
        titulo=f"{rotulo_met} por {rotulo_gran.lower()} - {p.rotulo}",
        colunas=[("rotulo", rotulo_gran), ("valor", rotulo_met)],
        linhas=linhas,
        formato={"valor": tipo},
        metadados={
            "periodo": p.rotulo,
            "de": p.inicio,
            "ate": p.fim,
            "metrica": metrica,
            "granularidade": chave_gran,
            "filtros": filtros or {},
        },
        grafico={
            "tipo": "linha",
            "x": "rotulo",
            "y": "valor",
            "titulo": f"{rotulo_met} por {rotulo_gran.lower()}",
        },
    )


def ranking(
    dimensao: str,
    periodo: str | None = None,
    metrica: str = "receita",
    top_n: int = 10,
    filtros: dict | None = None,
) -> Resultado:
    expr_dim, rotulo_dim = _dimensao(dimensao)
    p = _periodo(periodo)
    expr_met, rotulo_met, tipo = _metrica(metrica)
    onde, params = _where(p, filtros)
    try:
        top_n = max(1, min(int(top_n or 10), 100))
    except (TypeError, ValueError) as e:
        raise ErroFerramenta(f'top_n invalido: {top_n!r}') from e

    _, linhas = db.consultar(
        f"SELECT {expr_dim} AS rotulo, {expr_met} AS valor "
        f"FROM v_vendas WHERE {onde} GROUP BY {expr_dim} "
        f"ORDER BY valor DESC LIMIT ?",
        params + [top_n],
    )
    if (dimensao or "").strip().lower() == "dia_semana":
        linhas = _rotular_dias(linhas)

    total_periodo = None
    if metrica in METRICAS_ADITIVAS and linhas:
        _, total = db.consultar(
            f"SELECT {expr_met} AS valor FROM v_vendas WHERE {onde}", params
        )
        soma = (total[0].get("valor") or 0) if total else 0
        total_periodo = soma
        if soma:
            for linha in linhas:
                linha["participacao"] = (linha.get("valor") or 0) * 100.0 / soma
        colunas = [
            ("rotulo", rotulo_dim),
            ("valor", rotulo_met),
            ("participacao", "% do total"),
        ]
        formato = {"valor": tipo, "participacao": "pct"}
    else:
        colunas = [("rotulo", rotulo_dim), ("valor", rotulo_met)]
        formato = {"valor": tipo}

    return Resultado(
        titulo=f"{rotulo_met} por {rotulo_dim.lower()} | top {top_n} | {p.rotulo}",
        colunas=colunas,
        linhas=linhas,
        formato=formato,
        metadados={
            "periodo": p.rotulo,
            "dimensao": dimensao,
            "metrica": metrica,
            "top_n": top_n,
            "filtros": filtros or {},
            "total": total_periodo,
        },
        grafico={
            "tipo": "barra",
            "x": "rotulo",
            "y": "valor",
            "titulo": f"{rotulo_met} por {rotulo_dim.lower()}",
        },
    )


def distribuicao(
    dimensao: str,
    periodo: str | None = None,
    metrica: str = "receita",
    filtros: dict | None = None,
) -> Resultado:
    expr_dim, rotulo_dim = _dimensao(dimensao)
    expr_met, rotulo_met, tipo = _metrica(metrica)
    p = _periodo(periodo)
    onde, params = _where(p, filtros)

    _, linhas = db.consultar(
        f"SELECT {expr_dim} AS rotulo, {expr_met} AS valor "
        f"FROM v_vendas WHERE {onde} GROUP BY {expr_dim} "
        "ORDER BY valor DESC LIMIT 100",
        params,
    )
    if (dimensao or "").strip().lower() == "dia_semana":
        linhas = _rotular_dias(linhas)

    colunas = [("rotulo", rotulo_dim), ("valor", rotulo_met)]
    formato = {"valor": tipo}
    total_periodo = None
    if metrica in METRICAS_ADITIVAS:
        _, total = db.consultar(
            f"SELECT {expr_met} AS valor FROM v_vendas WHERE {onde}", params
        )
        soma = (total[0].get("valor") or 0) if total else 0
        total_periodo = soma
        if soma:
            for linha in linhas:
                linha["participacao"] = (linha.get("valor") or 0) * 100.0 / soma
            colunas.append(("participacao", "% do total"))
            formato["participacao"] = "pct"

    return Resultado(
        titulo=f"{rotulo_met} por {rotulo_dim.lower()} - {p.rotulo}",
        colunas=colunas,
        linhas=linhas,
        formato=formato,
        metadados={
            "periodo": p.rotulo,
            "dimensao": dimensao,
            "metrica": metrica,
            "filtros": filtros or {},
            "limite": 100,
            "total": total_periodo,
        },
        grafico={
            "tipo": "barra",
            "x": "rotulo",
            "y": "valor",
            "titulo": f"{rotulo_met} por {rotulo_dim.lower()}",
        },
    )


def comparar(
    periodo_a: str,
    periodo_b: str,
    metrica: str = "receita",
    filtros: dict | None = None,
) -> Resultado:
    expr_met, rotulo_met, tipo = _metrica(metrica)
    pa = _periodo(periodo_a)
    pb = _periodo(periodo_b)

    def _valor(p: periodo.Periodo) -> float | None:
        onde, params = _where(p, filtros)
        _, linhas = db.consultar(
            f"SELECT {expr_met} AS valor FROM v_vendas WHERE {onde}", params
        )
        return (linhas[0].get("valor") if linhas else None)

    va, vb = _valor(pa), _valor(pb)
    delta = None if va is None or vb is None else vb - va
    delta_pct = (
        None if delta is None or not va else delta * 100.0 / abs(va)
    )
    return Resultado(
        titulo=f"{rotulo_met}: {pa.rotulo} vs {pb.rotulo}",
        colunas=[
            ("a", pa.curto),
            ("b", pb.curto),
            ("delta", "Variacao"),
            ("delta_pct", "Variacao %"),
        ],
        linhas=[{"a": va, "b": vb, "delta": delta, "delta_pct": delta_pct}],
        formato={"a": tipo, "b": tipo, "delta": tipo, "delta_pct": "pct"},
        metadados={
            "metrica": metrica,
            "periodo_a": pa.rotulo,
            "periodo_b": pb.rotulo,
            "de_a": pa.inicio,
            "ate_a": pa.fim,
            "de_b": pb.inicio,
            "ate_b": pb.fim,
            "filtros": filtros or {},
        },
        grafico={
            "tipo": "comparacao",
            "x": "rotulo",
            "y": "valor",
            "titulo": f"{rotulo_met}: {pa.rotulo} vs {pb.rotulo}",
            "dados": [
                {"rotulo": pa.curto, "valor": va},
                {"rotulo": pb.curto, "valor": vb},
            ],
        },
    )


def tabela(
    dimensao: str,
    periodo: str | None = None,
    filtros: dict | None = None,
    limite: int = 50,
) -> Resultado:
    expr_dim, rotulo_dim = _dimensao(dimensao)
    p = _periodo(periodo)
    onde, params = _where(p, filtros)
    _, linhas = db.consultar(
        f"SELECT {expr_dim} AS rotulo, SUM(receita) AS receita, "
        "SUM(custo_total) AS custo, SUM(margem) AS margem, "
        "100.0 * SUM(margem) / NULLIF(SUM(receita), 0) AS margem_pct, "
        "COUNT(*) AS vendas "
        f"FROM v_vendas WHERE {onde} GROUP BY {expr_dim} "
        f"ORDER BY receita DESC LIMIT ?",
        params + [max(1, min(int(limite or 50), 200))],
    )
    if (dimensao or "").strip().lower() == "dia_semana":
        linhas = _rotular_dias(linhas)
    _, total = db.consultar(
        f"SELECT SUM(receita) AS valor FROM v_vendas WHERE {onde}", params
    )
    total_periodo = (total[0].get("valor") or 0) if total else 0
    return Resultado(
        titulo=f"{rotulo_dim} - {p.rotulo}",
        colunas=[
            ("rotulo", rotulo_dim),
            ("receita", "Receita"),
            ("custo", "Custo"),
            ("margem", "Margem"),
            ("margem_pct", "Margem %"),
            ("vendas", "Vendas"),
        ],
        linhas=linhas,
        formato={
            "receita": "brl",
            "custo": "brl",
            "margem": "brl",
            "margem_pct": "pct",
            "vendas": "num",
        },
        metadados={
            "periodo": p.rotulo,
            "dimensao": dimensao,
            "metrica": "receita",
            "filtros": filtros or {},
            "total": total_periodo,
        },
        grafico={
            "tipo": "barra",
            "x": "rotulo",
            "y": "receita",
            "titulo": f"Receita por {rotulo_dim.lower()}",
        },
    )


def _grafico_inferido(colunas: list[str], linhas: list[dict]) -> dict | None:
    """Chute honesto para SQL livre: linha se o eixo parece data, senao barra."""
    if len(colunas) < 2 or not linhas:
        return None
    eixo_y = None
    for chave in reversed(colunas[1:]):
        amostra = [linha.get(chave) for linha in linhas[:20]]
        if all(isinstance(v, (int, float)) for v in amostra):
            eixo_y = chave
            break
    if not eixo_y:
        return None
    primeiro = str(linhas[0].get(colunas[0]) or "")
    tipo = "linha" if re.match(r"^\d{4}(-\d{2})?$", primeiro) else "barra"
    return {"tipo": tipo, "x": colunas[0], "y": eixo_y, "titulo": "Resultado"}


def consultar_sql(sql: str) -> Resultado:
    """Escape hatch: SQL validado por `db.validar_sql`."""
    try:
        colunas, linhas, final = db.executar_sql(sql)
    except db.ErroSQL as e:
        raise ErroFerramenta(f"SQL recusado: {e}") from e
    except db.ErroConsulta as e:
        raise ErroFerramenta(f"SQL falhou: {e}") from e
    return Resultado(
        titulo="Consulta livre",
        colunas=[(c, c) for c in colunas],
        linhas=linhas,
        metadados={"sql": final, "linhas": len(linhas)},
        grafico=_grafico_inferido(colunas, linhas),
    )


# ---------------------------------------------------------------------------
# Catalogo (usado pelo contrato JSON e pelo CLI)
# ---------------------------------------------------------------------------

CATALOGO: dict[str, dict] = {
    "info_base": {
        "funcao": info_base,
        "descricao": "periodo coberto, contagens e totais da base",
        "args": {},
    },
    "resumo": {
        "funcao": resumo,
        "descricao": "receita, custo, margem, margem %, vendas, quantidade e ticket",
        "args": {"periodo": "str", "filtros": "dict"},
    },
    "serie": {
        "funcao": serie,
        "descricao": "evolucao de uma metrica ao longo do tempo",
        "args": {
            "periodo": "str",
            "granularidade": "enum:" + ",".join(sorted(GRANULARIDADES)),
            "metrica": "enum:" + ",".join(sorted(METRICAS)),
            "filtros": "dict",
        },
    },
    "ranking": {
        "funcao": ranking,
        "descricao": "top N de uma dimensao por uma metrica",
        "args": {
            "dimensao": "enum:" + ",".join(sorted(DIMENSOES)),
            "periodo": "str",
            "metrica": "enum:" + ",".join(sorted(METRICAS)),
            "top_n": "int",
            "filtros": "dict",
        },
    },
    "distribuicao": {
        "funcao": distribuicao,
        "descricao": "participacao de cada valor de uma dimensao",
        "args": {
            "dimensao": "enum:" + ",".join(sorted(DIMENSOES)),
            "periodo": "str",
            "metrica": "enum:" + ",".join(sorted(METRICAS)),
            "filtros": "dict",
        },
    },
    "comparar": {
        "funcao": comparar,
        "descricao": "compara uma metrica entre dois periodos",
        "args": {
            "periodo_a": "str",
            "periodo_b": "str",
            "metrica": "enum:" + ",".join(sorted(METRICAS)),
            "filtros": "dict",
        },
    },
    "tabela": {
        "funcao": tabela,
        "descricao": "receita, custo, margem e vendas por dimensao",
        "args": {
            "dimensao": "enum:" + ",".join(sorted(DIMENSOES)),
            "periodo": "str",
            "filtros": "dict",
            "limite": "int",
        },
    },
    "consultar_sql": {
        "funcao": consultar_sql,
        "descricao": "SELECT livre sobre v_vendas/v_mensal (ultimo recurso)",
        "args": {"sql": "str"},
    },
}


def executar(nome: str, args: dict | None = None) -> Resultado:
    """Chama a tool pelo nome, validando o nome contra o catalogo."""
    item = CATALOGO.get((nome or "").strip().lower())
    if not item:
        raise ErroFerramenta(
            f'ferramenta "{nome}" nao existe. Use: {", ".join(sorted(CATALOGO))}'
        )
    args = args or {}
    try:
        resultado = item["funcao"](**args)
    except TypeError as e:
        raise ErroFerramenta(f"argumentos invalidos para {nome}: {e}") from e

    intervalo = base()
    if intervalo and "de" in resultado.metadados:
        de = date.fromisoformat(resultado.metadados["de"])
        ate = date.fromisoformat(resultado.metadados["ate"])
        resultado.metadados["base"] = f"{intervalo[0]} a {intervalo[1]}"
        resultado.metadados["fora_da_base"] = not (de <= intervalo[1] and ate >= intervalo[0])
    return resultado


def catalogo_para_llm() -> str:
    """Descricao enxuta do catalogo para o prompt do modelo."""
    linhas = []
    for nome, item in CATALOGO.items():
        args = ", ".join(item["args"]) or "-"
        linhas.append(f"- {nome}({args}): {item['descricao']}")
    return "\n".join(linhas)