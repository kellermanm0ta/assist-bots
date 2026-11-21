#!/usr/bin/env python3
"""Datas e intervalos em portugues -> `Periodo(de, ate)`.

Deterministico e testavel, sem LLM. Aceita o que as pessoas escrevem no chat:
"2024", "marco de 2024", "1o trimestre de 2023", "ultimos 30 dias",
"de 01/03/2024 a 15/04/2024", "mes passado", "tudo".

Sem ano explicito, usa o ultimo ano da base (nao o ano corrente): a base do
analista cobre um periodo fixo e "marco" deve significar marco dentro dela.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta

MESES = {
    "janeiro": 1, "jan": 1,
    "fevereiro": 2, "fev": 2,
    "marco": 3, "mar": 3,
    "abril": 4, "abr": 4,
    "maio": 5, "mai": 5,
    "junho": 6, "jun": 6,
    "julho": 7, "jul": 7,
    "agosto": 8, "ago": 8,
    "setembro": 9, "set": 9,
    "outubro": 10, "out": 10,
    "novembro": 11, "nov": 11,
    "dezembro": 12, "dez": 12,
}
_MES_RE = "|".join(sorted(MESES, key=len, reverse=True))


class ErroPeriodo(ValueError):
    """Nao consegui transformar o texto num intervalo."""


@dataclass(frozen=True)
class Periodo:
    de: date
    ate: date
    rotulo: str = ""

    @property
    def inicio(self) -> str:
        return self.de.isoformat()

    @property
    def fim(self) -> str:
        return self.ate.isoformat()

    @property
    def dias(self) -> int:
        return (self.ate - self.de).days + 1

    @property
    def curto(self) -> str:
        """Rotulo compacto para eixos e cabecalhos: '2024', '03/2024'."""
        if self.de == date(self.de.year, 1, 1) and self.ate == date(self.de.year, 12, 31):
            return str(self.de.year)
        if (
            self.de.day == 1
            and self.de.month == self.ate.month
            and self.ate == _fim_mes(self.de.year, self.de.month)
        ):
            return f"{self.de.month:02d}/{self.de.year}"
        if self.de == self.ate:
            return self.de.strftime("%d/%m/%Y")
        return self.rotulo

    def sobrepoe(self, base: tuple[date, date] | None) -> bool:
        if not base:
            return True
        return self.de <= base[1] and self.ate >= base[0]


def limpar(texto: str | None) -> str:
    """minusculo, sem acento, 'º'/'°' -> 'o', espacos colapsados."""
    s = unicodedata.normalize("NFKD", str(texto or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("º", "o").replace("°", "o").lower()
    s = re.sub(r"\s+", " ", s)
    return s.strip(" .")


def _fim_mes(ano: int, mes: int) -> date:
    if mes == 12:
        return date(ano, 12, 31)
    return date(ano, mes + 1, 1) - timedelta(days=1)


def _mes_intervalo(ano: int, mes: int) -> tuple[date, date]:
    return date(ano, mes, 1), _fim_mes(ano, mes)


def _trimestre_intervalo(ano: int, tri: int) -> tuple[date, date]:
    primeiro = 3 * (tri - 1) + 1
    return date(ano, primeiro, 1), _fim_mes(ano, primeiro + 2)


def _semestre_intervalo(ano: int, sem: int) -> tuple[date, date]:
    if sem == 1:
        return date(ano, 1, 1), date(ano, 6, 30)
    return date(ano, 7, 1), date(ano, 12, 31)


def _data(txt: str, ano_padrao: int) -> date | None:
    """dd/mm/aaaa, aaaa-mm-dd, dd/mm (ano padrao) e aaaa-mm-dd parcial."""
    for formato, precisa_ano in (("%Y-%m-%d", False), ("%d/%m/%Y", False)):
        try:
            return datetime.strptime(txt, formato).date()
        except ValueError:
            pass
    m = re.match(r"^(\d{1,2})/(\d{1,2})$", txt)
    if m:
        try:
            return date(ano_padrao, int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    return None


def extremo(txt: str, ano_padrao: int) -> tuple[date, date] | None:
    """Converte um lado do intervalo ("marco", "2024", "1o tri 2023") em span."""
    t = limpar(txt)
    if not t:
        return None

    d = _data(t, ano_padrao)
    if d:
        return d, d

    # 03/2024
    m = re.match(r"^(\d{1,2})/(\d{4})$", t)
    if m:
        mes, ano = int(m.group(1)), int(m.group(2))
        if 1 <= mes <= 12:
            return _mes_intervalo(ano, mes)

    # 2024
    m = re.match(r"^(\d{4})$", t)
    if m:
        ano = int(m.group(1))
        return date(ano, 1, 1), date(ano, 12, 31)

    # 1o trimestre [de 2024] | t1 2024 | 1t2024 | 1 tri 2024
    m = re.match(
        rf"^(?:(\d+)\s*(?:o\s*)?(?:trimestre|tri|t)\s*(?:de\s*)?(\d{{4}})?"
        rf"|t\s*(\d)\s*(?:de\s*)?(\d{{4}})?)$",
        t,
    )
    if m:
        tri = int(m.group(1) or m.group(3))
        ano = int(m.group(2) or m.group(4) or ano_padrao)
        if 1 <= tri <= 4:
            return _trimestre_intervalo(ano, tri)

    # 1o semestre [de 2024]
    m = re.match(
        rf"^(\d+)\s*(?:o\s*)?semestre\s*(?:de\s*)?(\d{{4}})?$", t
    )
    if m:
        sem = int(m.group(1))
        ano = int(m.group(2) or ano_padrao)
        if 1 <= sem <= 2:
            return _semestre_intervalo(ano, sem)

    # marco [de 2024] | marco/2024
    m = re.match(rf"^({_MES_RE})\s*(?:de\s+|\s*/\s*)?(\d{{4}})?$", t)
    if m:
        mes = MESES[m.group(1)]
        ano = int(m.group(2) or ano_padrao)
        return _mes_intervalo(ano, mes)

    return None


def _anos(texto: str) -> set[int]:
    return {int(a) for a in re.findall(r"\b(1[89]\d{2}|20\d{2}|21\d{2})\b", texto)}


def _relativo(t: str, ref: date) -> tuple[date, date] | None:
    if t == "hoje":
        return ref, ref
    if t == "ontem":
        d = ref - timedelta(days=1)
        return d, d
    if t == "anteontem":
        d = ref - timedelta(days=2)
        return d, d

    if t in ("esta semana", "semana atual", "essa semana"):
        ini = ref - timedelta(days=ref.weekday())
        return ini, ini + timedelta(days=6)
    if t in ("semana passada", "ultima semana"):
        ini = ref - timedelta(days=ref.weekday() + 7)
        return ini, ini + timedelta(days=6)

    if t in ("este mes", "mes atual", "esse mes"):
        return _mes_intervalo(ref.year, ref.month)
    if t in ("mes passado", "mes anterior", "ultimo mes"):
        ano, mes = (ref.year, ref.month - 1) if ref.month > 1 else (ref.year - 1, 12)
        return _mes_intervalo(ano, mes)

    if t in ("este ano", "ano atual", "esse ano"):
        return date(ref.year, 1, 1), date(ref.year, 12, 31)
    if t in ("ano passado", "ano anterior"):
        return date(ref.year - 1, 1, 1), date(ref.year - 1, 12, 31)

    m = re.match(
        r"^(?:nos\s+)?(?:ultimos|ultimas|ultimo|ultima)\s+(\d+)?\s*"
        r"(dias?|semanas?|meses|mes|anos?|trimestres?)$",
        t,
    )
    if m:
        qtd = int(m.group(1) or 1)
        unidade = m.group(2)
        if unidade.startswith("dia"):
            ini = ref - timedelta(days=qtd - 1)
        elif unidade.startswith("semana"):
            ini = ref - timedelta(weeks=qtd)
        elif unidade.startswith("mes"):
            total = ref.year * 12 + (ref.month - 1) - qtd
            ini = date(total // 12, total % 12 + 1, 1)
        elif unidade.startswith("ano"):
            ini = date(ref.year - qtd, 1, 1)
        else:  # trimestre
            tri_atual = (ref.month - 1) // 3
            total = ref.year * 4 + tri_atual - qtd
            ini = date(total // 4, (total % 4) * 3 + 1, 1)
        return ini, ref

    m = re.match(r"^desde\s+(.+)$", t)
    if m:
        span = extremo(m.group(1), ref.year)
        if span:
            return span[0], None  # type: ignore[return-value]
    return None


def resolver(
    texto: str | None,
    base: tuple[date, date] | None = None,
    ref: date | None = None,
) -> Periodo:
    """Texto pt-BR -> Periodo. Sem texto, devolve a base inteira.

    `base` e o intervalo coberto pelo artefato (meta.data_min/data_max) e serve
    de fallback para "marco" (ano) e para "tudo".
    """
    ref = ref or date.today()
    if base:
        ref = min(max(ref, base[0]), base[1])
    ano_padrao = (base[1] if base else ref).year
    t = limpar(texto)

    if base and (not t or t in {
        "tudo", "total", "geral", "todo o periodo", "todo periodo", "todos",
        "base", "base inteira", "historico",
    }):
        return _com_rotulo(base[0], base[1])

    # faixas: "de X a Y", "entre X e Y", "X ate Y"
    corpo = re.sub(r"^(de|entre|desde)\s+", "", t)
    partes = re.split(r"\s+(?:a|ate|e)\s+", corpo)
    if len(partes) == 2:
        anos = _anos(t)
        ano = anos.pop() if len(anos) == 1 else ano_padrao
        a = extremo(partes[0], ano)
        b = extremo(partes[1], ano)
        if a and b:
            de, ate = a[0], b[1]
            if de > ate:
                de, ate = b[0], a[1]
            return _com_rotulo(de, ate)

    rel = _relativo(t, ref)
    if rel and rel[1] is None:  # "desde X"
        return _com_rotulo(rel[0], (base[1] if base else ref))
    if rel:
        return _com_rotulo(rel[0], rel[1])

    span = extremo(t, ano_padrao)
    if span:
        return _com_rotulo(span[0], span[1])

    raise ErroPeriodo(
        f'nao entendi o periodo "{texto}". Exemplos: 2024, marco de 2024, '
        "1o trimestre de 2023, ultimos 30 dias, de 01/03 a 15/04, tudo"
    )


def _com_rotulo(de: date, ate: date) -> Periodo:
    return Periodo(de=de, ate=ate, rotulo=f"{de:%d/%m/%Y} a {ate:%d/%m/%Y}")