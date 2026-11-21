#!/usr/bin/env python3
"""Gera PNG de um `Resultado` para o Telegram.

matplotlib com backend `Agg` (sem display), importado sob demanda: o bot sobe
rapido e continua funcionando com `GRAFICO_ENABLED=false` mesmo sem a lib.

Tipos: `linha` (serie temporal), `barra` (ranking horizontal), `comparacao`
(duas barras) e `pizza`/`rosca` (participacao, com agregacao em "Outros").
O desenho e deterministico a partir de `Resultado.grafico`; o tipo pode ser
sobrescrito por `gerar(..., tipo="pizza")` (botao e comandos).
"""
from __future__ import annotations

import os
import secrets
import time
from pathlib import Path

from analista import config
from analista.resultado import METRICAS_ADITIVAS, Resultado
from common import logsetup

log = logsetup.get_logger("graficos")

RETENCAO_DIAS = 7
MAX_BARRAS = 20
TIPOS = {"linha", "barra", "comparacao", "pizza", "rosca"}
TIPOS_PIZZA = {"pizza", "rosca"}

_plt = None


class ErroGrafico(RuntimeError):
    """Nao ha o que desenhar ou a combinacao pedida nao faz sentido."""


def _pyplot():
    global _plt
    if _plt is None:
        # Sem HOME de escrita (uid 1000 no container), o matplotlib cai num cache
        # temporario a cada processo e loga aviso. Fixamos no volume do agente.
        os.environ.setdefault("MPLCONFIGDIR", str(config.STATE_DIR / ".mpl"))
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        _plt = plt
    return _plt


def _milhar(valor, _pos=None) -> str:
    """Rotulo de eixo/fatia: 1234 -> '1,2 mil'; 1234567 -> '1,2 mi'."""
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


def _com_unidade(valor, formato: str | None) -> str:
    if formato == "brl":
        return "R$ " + _milhar(valor)
    if formato == "pct":
        return _milhar(valor) + "%"
    return _milhar(valor)


def _eixo_y(formato: str | None):
    plt = _pyplot()
    if formato == "brl":
        return plt.FuncFormatter(lambda v, p: "R$ " + _milhar(v, p))
    if formato == "pct":
        return plt.FuncFormatter(lambda v, p: _milhar(v, p) + "%")
    return plt.FuncFormatter(_milhar)


def _encurtar(texto: str, limite: int = 30) -> str:
    return texto if len(texto) <= limite else texto[: limite - 1] + "…"


def _limpar_antigos() -> None:
    """Apaga PNGs com mais de RETENCAO_DIAS para o volume nao crescer sem fim."""
    limite = time.time() - RETENCAO_DIAS * 86400
    try:
        for arquivo in config.GRAFICO_DIR.glob("*.png"):
            if arquivo.stat().st_mtime < limite:
                arquivo.unlink(missing_ok=True)
    except OSError as e:
        log.warning("falha limpando graficos antigos: %r", e)


def _novo_caminho(prefixo: str) -> Path:
    config.GRAFICO_DIR.mkdir(parents=True, exist_ok=True)
    slug = "".join(c if c.isalnum() else "-" for c in prefixo.lower())[:30].strip("-")
    return config.GRAFICO_DIR / f"{slug or 'grafico'}-{int(time.time())}-{secrets.token_hex(3)}.png"


# ---------------------------------------------------------------------------
# Pizza / rosca
# ---------------------------------------------------------------------------

def pode_pizza(res: Resultado) -> bool:
    """Pizza exige categorias, metrica somavel e valores nao negativos."""
    espec = res.grafico or {}
    if espec.get("tipo") not in {"barra"} | TIPOS_PIZZA:
        return False
    metrica = res.metadados.get("metrica")
    if metrica and metrica not in METRICAS_ADITIVAS:
        return False
    chave = espec.get("y", "valor")
    valores = [float(linha.get(chave) or 0) for linha in res.linhas]
    if not valores or any(v < 0 for v in valores):
        return False
    return sum(valores) > 0


def cobertura(res: Resultado, limite: int | None = None) -> float | None:
    """Quanto (%) do total do periodo as fatias mostradas cobrem.

    Serve para avisar quando a dimensao tem cauda longa (ex: cliente).
    """
    total = res.metadados.get("total")
    if not total:
        return None
    chave = (res.grafico or {}).get("y", "valor")
    valores = sorted((float(linha.get(chave) or 0) for linha in res.linhas), reverse=True)
    teto = limite or max(2, config.GRAFICO_MAX_FATIAS)
    return 100.0 * sum(valores[:teto]) / float(total)


def _validar_pizza(res: Resultado) -> None:
    metrica = res.metadados.get("metrica")
    if metrica and metrica not in METRICAS_ADITIVAS:
        raise ErroGrafico(
            f'pizza nao faz sentido para "{metrica}": a metrica precisa ser somavel'
        )
    if not res.linhas:
        raise ErroGrafico("sem dados no periodo/filtros informados")
    chave = (res.grafico or {}).get("y", "valor")
    valores = [float(linha.get(chave) or 0) for linha in res.linhas]
    if any(v < 0 for v in valores):
        raise ErroGrafico("pizza nao faz sentido com valores negativos")
    if sum(v for v in valores if v > 0) <= 0:
        raise ErroGrafico("sem valores positivos para desenhar")


def _dados_pizza(res: Resultado, espec: dict) -> tuple[list[tuple[str, float]], float]:
    """Top N fatias + "Outros" calculado contra o total do periodo."""
    chave_x = espec.get("x", "rotulo")
    chave_y = espec.get("y", "valor")
    pares = [
        (str(linha.get(chave_x)), float(linha.get(chave_y) or 0))
        for linha in res.linhas
        if float(linha.get(chave_y) or 0) > 0
    ]
    pares.sort(key=lambda par: par[1], reverse=True)
    if not pares:
        return [], 0.0

    total = float(res.metadados.get("total") or 0) or sum(valor for _, valor in pares)
    teto = max(2, config.GRAFICO_MAX_FATIAS)
    mostrados = pares[:teto]
    resto = total - sum(valor for _, valor in mostrados)
    if resto > max(total * 0.001, 1e-9):
        mostrados.append(("Outros", resto))
    return mostrados, total


def _gerar_pizza(res: Resultado, espec: dict, caminho: Path | None, tipo: str) -> Path:
    plt = _pyplot()
    pares, total = _dados_pizza(res, espec)
    if not pares:
        raise ErroGrafico("sem valores positivos para desenhar")

    formato = res.formato.get(espec.get("y", "valor"))
    titulo = espec.get("titulo") or res.titulo
    if total:
        titulo += f" - total {_com_unidade(total, formato)}"

    rotulos = [_encurtar(rotulo, 24) for rotulo, _ in pares]
    valores = [valor for _, valor in pares]

    fig, ax = plt.subplots(figsize=(8.6, 4.9), dpi=110)
    cmap = plt.get_cmap("tab10")
    cores = [cmap(i % 10) for i in range(len(pares))]
    if rotulos[-1] == "Outros":
        cores[-1] = (0.62, 0.64, 0.68, 1.0)

    fatias, _, _ = ax.pie(
        valores,
        colors=cores,
        startangle=90,
        counterclock=False,
        autopct=lambda p: f"{p:.1f}%" if p >= 5 else "",
        pctdistance=0.74,
        textprops={"fontsize": 8},
    )
    if tipo == "rosca":
        for fatia in fatias:
            fatia.set_width(0.42)

    legendas = []
    for (rotulo, valor), curto in zip(pares, rotulos):
        parte = f"{100 * valor / total:.1f}%" if total else ""
        legendas.append(f"{curto} - {_com_unidade(valor, formato)} ({parte})".strip())

    ax.legend(
        fatias,
        legendas,
        loc="center left",
        bbox_to_anchor=(0.98, 0.5),
        fontsize=7.5,
        frameon=False,
    )
    ax.set_aspect("equal")
    ax.set_title(titulo, fontsize=11, loc="left")
    fig.subplots_adjust(left=0.01, right=0.57, top=0.90, bottom=0.04)

    destino = _salvar(fig, caminho or _novo_caminho(titulo))
    log.info("grafico %s gerado (%d fatias)", destino.name, len(pares))
    return destino


def _salvar(fig, destino: Path) -> Path:
    plt = _pyplot()
    destino.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(destino, format="png")
    plt.close(fig)
    destino.chmod(0o644)
    return destino


# ---------------------------------------------------------------------------
# Entrada principal
# ---------------------------------------------------------------------------

def gerar(res: Resultado, caminho: Path | None = None, tipo: str | None = None) -> Path:
    """Desenha o grafico sugerido pelo Resultado e devolve o caminho do PNG."""
    if not config.GRAFICO_ENABLED:
        raise ErroGrafico("graficos desativados (GRAFICO_ENABLED=false)")

    caminho = Path(caminho) if caminho else None
    espec = dict(res.grafico or {})
    if not espec or not espec.get("tipo"):
        raise ErroGrafico("este resultado nao tem grafico sugerido")
    if tipo:
        if tipo not in TIPOS:
            raise ErroGrafico(f'tipo de grafico desconhecido: "{tipo}"')
        espec["tipo"] = tipo
    tipo = espec["tipo"]

    if tipo in TIPOS_PIZZA:
        _validar_pizza(res)
        _limpar_antigos()
        return _gerar_pizza(res, espec, caminho, tipo)

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

    destino = _salvar(fig, caminho or _novo_caminho(titulo))
    log.info("grafico %s gerado (%d linhas)", destino.name, len(res.linhas))
    return destino