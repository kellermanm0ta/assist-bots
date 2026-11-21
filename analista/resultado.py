#!/usr/bin/env python3
"""Resultado padrao de uma tool: dados crus + como formata-los.

Separado para que `ferramentas.py` e `formato.py` possam se referenciar sem
import circular.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Resultado:
    titulo: str
    colunas: list[tuple[str, str]]  # (chave, rotulo)
    linhas: list[dict]
    formato: dict[str, str] = field(default_factory=dict)  # chave -> brl|pct|num|text
    metadados: dict = field(default_factory=dict)
    grafico: dict | None = None

    def valores(self, chave: str) -> list:
        return [linha.get(chave) for linha in self.linhas]

    def para_dict(self, limite: int = 60) -> dict:
        """Versao enxuta para mandar ao LLM (nunca a base inteira)."""
        return {
            "titulo": self.titulo,
            "colunas": [rotulo for _, rotulo in self.colunas],
            "linhas": self.linhas[:limite],
            "metadados": self.metadados,
        }