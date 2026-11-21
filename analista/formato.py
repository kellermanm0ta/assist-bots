#!/usr/bin/env python3
"""Formatacao pt-BR dos resultados: numeros, percentuais e tabelas monoespace.

Separado de `ferramentas.py` de proposito: as ferramentas devolvem dados crus
(float/int/str) e so aqui eles viram texto para o Telegram ou para o CLI.
"""
from __future__ import annotations

from analista.resultado import Resultado

TIPOS_NUMERICOS = {"brl", "pct", "num"}


def numero(valor, casas: int = 0) -> str:
    if valor is None:
        return "-"
    if casas == 0:
        return f"{valor:,.0f}".replace(",", ".")
    return f"{valor:,.{casas}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def brl(valor) -> str:
    return "R$ " + numero(valor, 2)


def pct(valor) -> str:
    return numero(valor, 1) + "%"


def fmt(tipo: str | None, valor) -> str:
    if valor is None:
        return "-"
    if tipo == "brl":
        return brl(valor)
    if tipo == "pct":
        return pct(valor)
    if tipo == "num":
        return numero(valor) if float(valor).is_integer() else numero(valor, 2)
    return str(valor)


def tabela(res: Resultado, limite: int = 25) -> str:
    """Tabela alinhada, pronta para `parse_mode="HTML"` + `<pre>`."""
    linhas = res.linhas[:limite]
    if not linhas:
        return "Sem dados no periodo/filtros informados."

    chaves = [c for c, _ in res.colunas]
    cabecalho = [rotulo for _, rotulo in res.colunas]
    corpo = [
        [fmt(res.formato.get(chave), linha.get(chave)) for chave in chaves]
        for linha in linhas
    ]
    larguras = [
        max(len(cabecalho[i]), *(len(linha[i]) for linha in corpo))
        for i in range(len(chaves))
    ]

    def montar(celulas: list[str]) -> str:
        partes = []
        for i, chave in enumerate(chaves):
            if res.formato.get(chave) in TIPOS_NUMERICOS:
                partes.append(celulas[i].rjust(larguras[i]))
            else:
                partes.append(celulas[i].ljust(larguras[i]))
        return "  ".join(partes).rstrip()

    saida = [montar(cabecalho), "  ".join("-" * largura for largura in larguras)]
    saida += [montar(linha) for linha in corpo]
    if len(res.linhas) > limite:
        saida.append(f"... e mais {len(res.linhas) - limite} linha(s).")
    return "\n".join(saida)


def resumo_texto(res: Resultado) -> str:
    """Uma linha por indicador (melhor que tabela larga no celular)."""
    if not res.linhas:
        return "Sem dados no periodo/filtros informados."
    linha = res.linhas[0]
    return "\n".join(
        f"{rotulo}: {fmt(res.formato.get(chave), linha.get(chave))}"
        for chave, rotulo in res.colunas
    )


def lista(res: Resultado, limite: int = 10) -> str:
    """Ranking como lista numerada, com participacao quando houver."""
    linhas = res.linhas[:limite]
    if not linhas:
        return "Sem dados no periodo/filtros informados."
    partes = []
    for i, linha in enumerate(linhas, start=1):
        rotulo = linha.get("rotulo")
        valor = fmt(res.formato.get("valor"), linha.get("valor"))
        extra = ""
        if linha.get("participacao") is not None:
            extra = f" ({pct(linha['participacao'])})"
        partes.append(f"{i}. {rotulo} - {valor}{extra}")
    return "\n".join(partes)