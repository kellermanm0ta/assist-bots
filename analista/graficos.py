#!/usr/bin/env python3
"""Gera PNG de um `Resultado` para o Telegram.

matplotlib com backend `Agg` (sem display), importado sob demanda: o bot sobe
rapido e continua funcionando com `GRAFICO_ENABLED=false` mesmo sem a lib.
O desenho e deterministico a partir da sugestao em `Resultado.grafico`.
"""
from __future__ import annotations

import secrets
import time
from pathlib import Path

from analista import config
from analista.resultado import Resultado
from common import logsetup

log = logsetup.get_logger("graficos")

RETENCAO_DIAS = 7
MAX_BARRAS = 20

_plt = None


class ErroGrafico(RuntimeError):
    """Nao ha o que desenhar ou falha ao renderizar."""


def _pyplot():
    global _plt
    if _plt is None:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        _plt = plt
    return _plt


def _milhar(valor, _pos=None) -> str:
    """Rotulo do eixo: 1.234 -> '1,2 mil'; 1234567 -> '1,2 mi'."""
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return ""
    for limite, sufixo in ((1_000_000_000, " bi"), (1_000_000, " mi"), (1_000, " mil")):
        if abs(numero) >= limite:
            texto = f"{numero / limite:,.1f}".replace(",", "X").replace(".", ",").replace("X", ".")
            return texto + sufixo
    if abs(numero - int(numero)) < 1e-9:
        return f"{int(numero)}"
    return f"{numero:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _eixo_y(formato: str | None):
    plt = _pyplot()
    if formato == "brl":
        return plt.FuncFormatter(lambda v, p: "R$ " + _milhar(v, p))
    if formato == "pct":
        return plt.FuncFormatter(lambda v, p: _milhar(v, p) + "%")
    return plt.FuncFormatter(_milhar)


def _limpar_antigos() -> None:
    """Apaga PNGs com mais de RETENCAO_DIAS para o volume nao crescer sem fim."""
    limite = time.time() - RETENCAO_DIAS * 86400
    try:
        for arquivo in config.GRAFICO_DIR.glob("*.png"):
            if arquivo.stat().st_mtime < limite:
                arquivo.unlink(missing_ok=True)
    except OSError as e:
        log.warning("falha limpando graficos antigos: %r", e)


def _encurtar(texto: str, limite: int = 30) -> str:
    return texto if len(texto) <= limite else texto[: limite - 1] + "…"


def _novo_caminho(prefixo: str) -> Path:
    config.GRAFICO_DIR.mkdir(parents=True, exist_ok=True)
    slug = "".join(c if c.isalnum() else "-" for c in prefixo.lower())[:30].strip("-")
    return config.GRAFICO_DIR / f"{slug or 'grafico'}-{int(time.time())}-{secrets.token_hex(3)}.png"


def gerar(res: Resultado, caminho: Path | None = None) -> Path:
    """Desenha o grafico sugerido pelo Resultado e devolve o caminho do PNG."""
    if not config.GRAFICO_ENABLED:
        raise ErroGrafico("graficos desativados (GRAFICO_ENABLED=false)")
    espec = res.grafico or {}
    tipo = espec.get("tipo") or ""
    if not espec or not tipo:
        raise ErroGrafico("este resultado nao tem grafico sugerido")
    if not res.linhas and not espec.get("dados"):
        raise ErroGrafico("sem dados no periodo/filtros informados")

    plt = _pyplot()
    _limpar_antigos()

    formato = res.formato.get(espec.get("y", "valor"))
    titulo = espec.get("titulo") or res.titulo

    fig, ax = plt.subplots(figsize=(8.0, 4.5), dpi=110)

    if tipo == "linha":
        rotulos = [str(linha.get(espec.get("x", "rotulo"))) for linha in res.linhas]
        valores = [linha.get(espec.get("y", "valor")) or 0 for linha in res.linhas]
        posicoes = list(range(len(rotulos)))
        ax.plot(posicoes, valores, linewidth=1.8, color="#1f6feb")
        ax.fill_between(posicoes, valores, color="#1f6feb", alpha=0.12)
        passo = max(1, len(rotulos) // 12)
        ax.set_xticks(posicoes[::passo])
        ax.set_xticklabels(rotulos[::passo], rotation=45, ha="right", fontsize=8)
        ax.grid(axis="y", alpha=0.25)
        eixo_valor = ax.yaxis

    elif tipo == "barra":
        recorte = res.linhas[:MAX_BARRAS]
        rotulos = [_encurtar(str(linha.get(espec.get("x", "rotulo")))) for linha in recorte]
        valores = [linha.get(espec.get("y", "valor")) or 0 for linha in recorte]
        posicoes = list(range(len(rotulos)))
        ax.barh(posicoes, valores, color="#1f6feb")
        ax.set_yticks(posicoes)
        ax.set_yticklabels(rotulos, fontsize=9)
        ax.invert_yaxis()
        ax.grid(axis="x", alpha=0.25)
        if len(res.linhas) > len(recorte):
            titulo += f" (top {len(recorte)})"
        eixo_valor = ax.xaxis

    elif tipo == "comparacao":
        dados = espec.get("dados") or []
        rotulos = [_encurtar(str(item.get("rotulo")), 22) for item in dados]
        valores = [item.get("valor") or 0 for item in dados]
        barras = ax.bar(range(len(rotulos)), valores, color=["#8b949e", "#1f6feb"])
        ax.bar_label(barras, fmt=lambda v: _milhar(v), padding=2, fontsize=9)
        ax.set_xticks(range(len(rotulos)))
        ax.set_xticklabels(rotulos, fontsize=9)
        ax.grid(axis="y", alpha=0.25)
        eixo_valor = ax.yaxis

    else:
        plt.close(fig)
        raise ErroGrafico(f'tipo de grafico desconhecido: "{tipo}"')

    eixo_valor.set_major_formatter(_eixo_y(formato))
    ax.set_title(titulo, fontsize=11, loc="left")
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()

    destino = caminho or _novo_caminho(titulo)
    destino.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(destino, format="png")
    plt.close(fig)
    destino.chmod(0o644)
    log.info("grafico %s gerado (%d linhas)", destino.name, len(res.linhas))
    return destino