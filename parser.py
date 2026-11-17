#!/usr/bin/env python3
"""Interpretacao de linguagem natural para lembretes (deterministica).

Reconhece, sem LLM, os formatos:

  me lembre em 18/11 as 8h de tomar remedio
  me lembre em 18/11 as 8h de tomar remedio, repetir todos os dias
  me lembre em 18/11 as 8h de academia todas as tercas
  me lembre todos os dias de tomar remedio
  me lembre todas as tercas as 7h de academia
  cancelar tomar remedio
  /cancelar a1b2c3

`interpretar()` devolve um `Resultado` e NUNCA persiste nada. A confirmacao,
com a data absoluta resolvida, e responsabilidade do bot. O LLM de fallback
(`runtime.py`) chama `de_campos()`, que revalida tudo com as mesmas regras.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from dateutil import rrule

import config

DIAS = {
    "segunda": "MO",
    "terca": "TU",
    "quarta": "WE",
    "quinta": "TH",
    "sexta": "FR",
    "sabado": "SA",
    "domingo": "SU",
}
DIAS_NOME = {v: k for k, v in DIAS.items()}
SEMANA_ABREV = ["seg", "ter", "qua", "qui", "sex", "sab", "dom"]
MESES = {
    "janeiro": 1,
    "fevereiro": 2,
    "marco": 3,
    "abril": 4,
    "maio": 5,
    "junho": 6,
    "julho": 7,
    "agosto": 8,
    "setembro": 9,
    "outubro": 10,
    "novembro": 11,
    "dezembro": 12,
}

_HORA_TOK = (
    r"(\d{1,2}\s*[:h]\s*\d{2}"
    r"|\d{1,2}\s*h(?:oras?)?"
    r"|meio[\s-]?dia"
    r"|meia[\s-]?noite"
    r"|\d{1,2})"
)

_DIAS_ALT = r"(segundas?|tercas?|quartas?|quintas?|sextas?|sabados?|domingos?)"

_CANCELAR = re.compile(
    r"^(?:cancelar|cancele|cancela|remover|remova|apagar|apaga|excluir|exclui)"
    r"(?:\s+(?:o|a|os|as))?"
    r"(?:\s+lembrete)?"
    r"(?:\s+de)?"
    r"\s+(?P<alvo>.+)$"
)

_ATALHO_DIARIO = re.compile(
    r"^me lembr[ea] (?:de )?"
    r"(?:todos os dias|todo dia|diariamente)"
    r"(?: as " + _HORA_TOK + r")?"
    r" (?:de|para|que) (?P<evento>.+)$"
)

_ATALHO_SEMANAL = re.compile(
    r"^me lembr[ea] (?:de )?"
    r"(?:todas as|toda) " + _DIAS_ALT + r"(?:-?\s*feiras?)?"
    r"(?: as " + _HORA_TOK + r")?"
    r" (?:de|para|que) (?P<evento>.+)$"
)

_ATALHO_MENSAL = re.compile(
    r"^me lembr[ea] (?:de )?(?:todo|todos) dias? (\d{1,2})"
    r"(?: as " + _HORA_TOK + r")?"
    r" (?:de|para|que) (?P<evento>.+)$"
)

_ATALHO_INTERVALO = re.compile(
    r"^me lembr[ea] (?:de )?a cada (\d+) (minutos?|horas?|dias?|semanas?|meses?)"
    r"(?: as " + _HORA_TOK + r")?"
    r" (?:de|para|que) (?P<evento>.+)$"
)

_ATALHO_UTEIS = re.compile(
    r"^me lembr[ea] (?:de )?(?:de segunda a sexta|dias uteis)"
    r"(?: as " + _HORA_TOK + r")?"
    r" (?:de|para|que) (?P<evento>.+)$"
)

_CRIAR = re.compile(
    r"^me lembr[ea] (?:em )?(?P<data>.+?) as "
    r"(?P<hora>" + _HORA_TOK + r")"
    r" (?:de|para|que) (?P<evento>.+)$"
)


@dataclass
class Resultado:
    tipo: str  # "criar" | "cancelar" | "erro"
    texto: str = ""
    primeira: datetime | None = None
    rrule: str | None = None
    mensagem: str | None = None


# ---------------------------------------------------------------------------
# Normalizacao / tokens
# ---------------------------------------------------------------------------

def _normalizar_mapa(texto: str) -> tuple[str, list[int]]:
    """Minusculas, sem acentos, pontuacao fraca -> espaco.

    Devolve tambem um mapa posicao-normalizada -> posicao-no-original, para
    recuperar o texto do evento com acentos/caixa originais.
    """
    out: list[str] = []
    mapa: list[int] = []
    for i, ch in enumerate(texto or ""):
        for c in unicodedata.normalize("NFKD", ch.lower()):
            if unicodedata.combining(c):
                continue
            if c.isalnum() or c == "_" or c in "/:":
                out.append(c)
                mapa.append(i)
            elif not out or out[-1] != " ":
                out.append(" ")
                mapa.append(i)
    s = "".join(out)
    ini = len(s) - len(s.lstrip())
    fim = len(s.rstrip())
    return s[ini:fim], mapa[ini:fim]


def normalizar(texto: str) -> str:
    return _normalizar_mapa(texto)[0]


def _evento(
    texto: str, mapa: list[int], inicio_norm: int, fim_norm: int | None = None
) -> str:
    inicio = mapa[inicio_norm] if inicio_norm < len(mapa) else len(texto)
    fim = mapa[fim_norm] if fim_norm is not None and fim_norm < len(mapa) else len(texto)
    return texto[inicio:fim].strip(" .,!?:;")


# ---------------------------------------------------------------------------
# Hora / data
# ---------------------------------------------------------------------------

def hora_padrao() -> time:
    try:
        h, m = config.HORA_PADRAO.split(":")
        return time(int(h), int(m))
    except Exception:
        return time(8, 0)


def _parse_hora(texto: str) -> time | None:
    s = normalizar(texto)
    if not s:
        return None
    if "meio dia" in s:
        return time(12, 0)
    if "meia noite" in s:
        return time(0, 0)
    m = re.fullmatch(r"(\d{1,2})\s*[:h]\s*(\d{2})", s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return time(h, mi)
        return None
    m = re.fullmatch(r"(\d{1,2})\s*(?:h|hora|horas)?", s)
    if m:
        h = int(m.group(1))
        if 0 <= h <= 23:
            return time(h, 0)
    return None


def _parse_data(texto: str, hoje: date) -> date | None:
    s = normalizar(texto)
    if not s:
        return None
    if s == "hoje":
        return hoje
    if s == "amanha":
        return hoje + timedelta(days=1)
    if s in ("depois de amanha", "depois de amanha mesmo"):
        return hoje + timedelta(days=2)

    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", s)
    if m:
        return _montar_data(int(m.group(1)), int(m.group(2)), m.group(3), hoje)

    m = re.fullmatch(
        r"(\d{1,2})\s+de\s+([a-z]+)(?:\s+de\s+(\d{2,4}))?", s
    )
    if m:
        mes = MESES.get(m.group(2))
        if mes:
            return _montar_data(int(m.group(1)), mes, m.group(3), hoje)
        return None

    m = re.fullmatch(r"dia\s+(\d{1,2})", s)
    if m:
        return _montar_data(int(m.group(1)), hoje.month, None, hoje)

    m = re.fullmatch(
        r"(?:proxima\s+|proximo\s+)?(segunda|terca|quarta|quinta|sexta|sabado|domingo)"
        r"(?:\s*-?\s*feira)?",
        s,
    )
    if m:
        alvo = list(DIAS).index(m.group(1))
        delta = (alvo - hoje.weekday()) % 7
        return hoje + timedelta(days=delta or 7)
    return None


def _montar_data(dia: int, mes: int, ano: str | None, hoje: date) -> date | None:
    try:
        if ano:
            y = int(ano)
            if y < 100:
                y += 2000
            return date(y, mes, dia)
        d = date(hoje.year, mes, dia)
        if d < hoje:  # data sem ano no passado -> proximo ano
            d = date(hoje.year + 1, mes, dia)
        return d
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Recorrencia no fim da frase
# ---------------------------------------------------------------------------

def _intervalo(m: re.Match) -> str | None:
    n = int(m.group(1))
    u = m.group(2)
    if u.startswith("minuto"):
        freq = "MINUTELY"
    elif u.startswith("hora"):
        freq = "HOURLY"
    elif u.startswith("dia"):
        freq = "DAILY"
    elif u.startswith("semana"):
        freq = "WEEKLY"
    elif u.startswith("mes"):
        freq = "MONTHLY"
    else:
        return None
    return f"FREQ={freq};INTERVAL={n}"


def _semanal(m: re.Match) -> str | None:
    singular = re.sub(r"s$", "", m.group(1))
    cod = DIAS.get(singular)
    return f"FREQ=WEEKLY;BYDAY={cod}" if cod else None


_REC_TAIL = [
    (
        rf"(?:repetir|repita|repetindo)?\s*(?:dias uteis|de segunda a sexta)$",
        lambda m: "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
    ),
    (
        rf"(?:repetir|repita|repetindo)?\s*(?:de|a cada)\s+(\d+)\s+"
        rf"(minutos?|horas?|dias?|semanas?|meses?)$",
        _intervalo,
    ),
    (
        rf"(?:repetir|repita|repetindo)?\s*(?:todo|todos)\s+dias?\s+(\d{{1,2}})$",
        lambda m: f"FREQ=MONTHLY;BYMONTHDAY={int(m.group(1))}",
    ),
    (
        rf"(?:repetir|repita|repetindo)?\s*(?:todas as|toda|todos os)\s+"
        rf"{_DIAS_ALT}(?:\s*-?\s*feiras?)?$",
        _semanal,
    ),
    (
        r"(?:repetir|repita|repetindo)?\s*(?:toda|todas as)\s+semanas?$",
        lambda m: "FREQ=WEEKLY",
    ),
    (
        r"(?:repetir|repita|repetindo)?\s*(?:todo|todos)\s+anos?$",
        lambda m: "FREQ=YEARLY",
    ),
    (
        r"(?:repetir|repita|repetindo)?\s*(?:todos os dias|todo dia|diariamente)$",
        lambda m: "FREQ=DAILY",
    ),
]


def _extrair_recorrencia(norm: str) -> tuple[str | None, str, int]:
    for padrao, build in _REC_TAIL:
        m = re.search(padrao, norm)
        if not m:
            continue
        rec = build(m)
        if not rec:
            continue
        sem = norm[: m.start()].strip()
        if sem.split():
            return rec, sem, m.start()
    return None, norm, len(norm)


def proxima_ocorrencia(rec: str, dtstart: datetime, depois: datetime) -> datetime | None:
    regra = rrule.rrulestr(rec, dtstart=dtstart)
    return regra.after(depois, inc=False)


def _proximo_dia_mes(dia: int, hora: time, agora: datetime) -> datetime | None:
    for offset in range(0, 14):
        ano = agora.year + (agora.month - 1 + offset) // 12
        mes = (agora.month - 1 + offset) % 12 + 1
        try:
            cand = datetime.combine(date(ano, mes, dia), hora)
        except ValueError:
            continue
        if cand > agora:
            return cand
    return None


# ---------------------------------------------------------------------------
# Interpretacao
# ---------------------------------------------------------------------------

def interpretar(texto: str, agora: datetime) -> Resultado:
    original = " ".join((texto or "").split())
    norm, mapa = _normalizar_mapa(original)
    if not norm:
        return Resultado("erro", mensagem="Mensagem vazia.")

    m = _CANCELAR.match(norm)
    if m:
        alvo = _evento(original, mapa, m.start("alvo"))
        if not alvo:
            return Resultado("erro", mensagem="Uso: cancelar <evento>")
        return Resultado("cancelar", texto=alvo)

    m = _ATALHO_DIARIO.match(norm)
    if m:
        hora = _parse_hora(m.group(1)) if m.group(1) else hora_padrao()
        if hora is None:
            return Resultado("erro", mensagem="Hora invalida.")
        evento = _evento(original, mapa, m.start("evento"))
        if not evento:
            return Resultado("erro", mensagem="Faltou o evento do lembrete.")
        primeira = datetime.combine(agora.date() + timedelta(days=1), hora)
        return Resultado("criar", texto=evento, primeira=primeira, rrule="FREQ=DAILY")

    m = _ATALHO_SEMANAL.match(norm)
    if m:
        singular = re.sub(r"s$", "", m.group(1))
        cod = DIAS.get(singular)
        if not cod:
            return Resultado("erro", mensagem="Dia da semana invalido.")
        hora = _parse_hora(m.group(2)) if m.group(2) else hora_padrao()
        if hora is None:
            return Resultado("erro", mensagem="Hora invalida.")
        evento = _evento(original, mapa, m.start("evento"))
        if not evento:
            return Resultado("erro", mensagem="Faltou o evento do lembrete.")
        alvo = list(DIAS).index(singular)
        delta = (alvo - agora.weekday()) % 7 or 7
        primeira = datetime.combine(agora.date() + timedelta(days=delta), hora)
        return Resultado(
            "criar", texto=evento, primeira=primeira, rrule=f"FREQ=WEEKLY;BYDAY={cod}"
        )

    m = _ATALHO_MENSAL.match(norm)
    if m:
        dia = int(m.group(1))
        hora = _parse_hora(m.group(2)) if m.group(2) else hora_padrao()
        evento = _evento(original, mapa, m.start("evento"))
        if hora is None:
            return Resultado("erro", mensagem="Hora invalida.")
        if not evento:
            return Resultado("erro", mensagem="Faltou o evento do lembrete.")
        primeira = _proximo_dia_mes(dia, hora, agora)
        if primeira is None:
            return Resultado("erro", mensagem="Dia do mes invalido.")
        return Resultado(
            "criar",
            texto=evento,
            primeira=primeira,
            rrule=f"FREQ=MONTHLY;BYMONTHDAY={dia}",
        )

    m = _ATALHO_INTERVALO.match(norm)
    if m:
        n = int(m.group(1))
        unidade = m.group(2)
        if unidade.startswith("minuto"):
            freq = "MINUTELY"
        elif unidade.startswith("hora"):
            freq = "HOURLY"
        elif unidade.startswith("dia"):
            freq = "DAILY"
        elif unidade.startswith("semana"):
            freq = "WEEKLY"
        else:
            freq = "MONTHLY"
        hora = _parse_hora(m.group(3)) if m.group(3) else None
        evento = _evento(original, mapa, m.start("evento"))
        if not evento:
            return Resultado("erro", mensagem="Faltou o evento do lembrete.")
        rec = f"FREQ={freq};INTERVAL={n}"
        base = agora if hora is None else datetime.combine(agora.date(), hora)
        if base <= agora:
            base = agora
        primeira = proxima_ocorrencia(rec, base, agora)
        if primeira is None:
            return Resultado("erro", mensagem="Nao ha proxima ocorrencia.")
        return Resultado("criar", texto=evento, primeira=primeira, rrule=rec)

    m = _ATALHO_UTEIS.match(norm)
    if m:
        hora = _parse_hora(m.group(1)) if m.group(1) else hora_padrao()
        evento = _evento(original, mapa, m.start("evento"))
        if hora is None:
            return Resultado("erro", mensagem="Hora invalida.")
        if not evento:
            return Resultado("erro", mensagem="Faltou o evento do lembrete.")
        rec = "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"
        base = datetime.combine(agora.date(), hora)
        primeira = proxima_ocorrencia(rec, base, agora)
        if primeira is None:
            return Resultado("erro", mensagem="Nao ha proxima ocorrencia.")
        return Resultado("criar", texto=evento, primeira=primeira, rrule=rec)

    rec, norm_sem, rec_start = _extrair_recorrencia(norm)
    m = _CRIAR.match(norm_sem)
    if not m:
        return Resultado(
            "erro",
            mensagem=(
                'Nao entendi. Tente: "me lembre em 18/11 as 8h de tomar remedio, '
                'repetir todos os dias" ou "me lembre todos os dias de tomar remedio".'
            ),
        )

    data = _parse_data(m.group("data"), agora.date())
    hora = _parse_hora(m.group("hora"))
    if data is None:
        return Resultado("erro", mensagem="Nao entendi a data.")
    if hora is None:
        return Resultado("erro", mensagem="Hora invalida.")

    evento = _evento(original, mapa, m.start("evento"), rec_start)
    if not evento:
        return Resultado("erro", mensagem="Faltou o evento do lembrete.")

    primeira = datetime.combine(data, hora)
    if primeira <= agora:
        if rec:
            avancada = proxima_ocorrencia(rec, primeira, agora)
            if avancada is None:
                return Resultado("erro", mensagem="Nao ha proxima ocorrencia.")
            primeira = avancada
        else:
            return Resultado(
                "erro", mensagem="Essa data e hora ja passaram. Escolha um horario futuro."
            )

    if rec and rec.startswith("FREQ="):
        try:
            rrule.rrulestr(rec, dtstart=primeira)
        except Exception:
            return Resultado("erro", mensagem="Recorrencia invalida.")
    return Resultado("criar", texto=evento, primeira=primeira, rrule=rec)


def de_campos(
    evento: str | None,
    data_iso: str | None,
    hora_str: str | None,
    rec: str | None,
    agora: datetime,
) -> Resultado:
    """Valida campos vindos do LLM de fallback com as mesmas regras."""
    evento = (evento or "").strip()
    if not evento:
        return Resultado("erro", mensagem="Faltou o evento do lembrete.")

    rec = (rec or "").strip().upper() or None
    if rec and not rec.startswith("FREQ="):
        return Resultado("erro", mensagem="Recorrencia invalida.")

    hora = _parse_hora(hora_str) if hora_str else hora_padrao()
    if hora is None:
        return Resultado("erro", mensagem="Hora invalida.")

    if data_iso:
        try:
            data = date.fromisoformat(data_iso)
        except ValueError:
            return Resultado("erro", mensagem="Data invalida.")
    elif rec:
        data = agora.date() + timedelta(days=1)
    else:
        return Resultado("erro", mensagem="Faltou a data do lembrete.")

    primeira = datetime.combine(data, hora)
    if rec:
        try:
            rrule.rrulestr(rec, dtstart=primeira)
        except Exception:
            return Resultado("erro", mensagem="Recorrencia invalida.")
        if primeira <= agora:
            avancada = proxima_ocorrencia(rec, primeira, agora)
            if avancada is None:
                return Resultado("erro", mensagem="Nao ha proxima ocorrencia.")
            primeira = avancada
    elif primeira <= agora:
        return Resultado("erro", mensagem="Essa data e hora ja passaram.")

    return Resultado("criar", texto=evento, primeira=primeira, rrule=rec)


# ---------------------------------------------------------------------------
# Formatacao para o usuario
# ---------------------------------------------------------------------------

def formatar(dt: datetime) -> str:
    return f"{dt:%d/%m/%Y} ({SEMANA_ABREV[dt.weekday()]}) as {dt:%H:%M}"


def descrever(rec: str | None) -> str:
    if not rec:
        return "uma vez"
    partes = {}
    for item in rec.split(";"):
        if "=" in item:
            k, v = item.split("=", 1)
            partes[k.strip().upper()] = v.strip().upper()
    freq = partes.get("FREQ", "")
    try:
        intervalo = int(partes.get("INTERVAL", "1"))
    except ValueError:
        intervalo = 1

    if freq == "DAILY":
        return "todos os dias" if intervalo == 1 else f"a cada {intervalo} dias"
    if freq == "WEEKLY":
        byday = partes.get("BYDAY")
        if byday == "MO,TU,WE,TH,FR":
            return "de segunda a sexta"
        if byday:
            nomes = [DIAS_NOME.get(c, c) for c in byday.split(",")]
            if len(nomes) == 1:
                return f"toda {nomes[0]}"
            return "toda " + ", ".join(nomes[:-1]) + " e " + nomes[-1]
        return "toda semana" if intervalo == 1 else f"a cada {intervalo} semanas"
    if freq == "MONTHLY":
        dia = partes.get("BYMONTHDAY")
        if dia:
            return f"todo dia {dia} do mes"
        return "todo mes" if intervalo == 1 else f"a cada {intervalo} meses"
    if freq == "YEARLY":
        return "todo ano"
    if freq == "HOURLY":
        return "toda hora" if intervalo == 1 else f"a cada {intervalo} horas"
    if freq == "MINUTELY":
        return f"a cada {intervalo} minutos"
    return rec