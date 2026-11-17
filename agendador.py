#!/usr/bin/env python3
"""Agendador: thread que dispara as notificacoes via Telegram.

Estrategia: sem cron e sem APScheduler. O estado fica em `lembretes.json`; a
cada ciclo o agendador procura lembretes vencidos, notifica e calcula a
proxima ocorrencia com `dateutil.rrule`. Um `Event` acorda a thread assim que
um lembrete e criado/cancelado, e o `sleep` e limitado a 30 s para reagir a
mudancas de relogio e a lembretes novos.
"""
from __future__ import annotations

import threading
from datetime import datetime

import config
import lembretes
import logsetup
import telegram_api
from parser import formatar, proxima_ocorrencia

log = logsetup.get_logger("agendador")


class Agendador(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True, name="agendador")
        self._wake = threading.Event()
        self._parar = threading.Event()

    def acordar(self) -> None:
        self._wake.set()

    def parar(self) -> None:
        self._parar.set()
        self._wake.set()

    def run(self) -> None:
        log.info("agendador iniciado (tz=%s tolerancia=%smin)", config.TIMEZONE, config.TOLERANCIA_MIN)
        while not self._parar.is_set():
            try:
                self._ciclo()
            except Exception as e:
                log.error("falha no ciclo do agendador: %r", e)
            self._wake.wait(self._espera())
            self._wake.clear()
        log.info("agendador encerrado")

    def _ciclo(self) -> None:
        agora = _agora()
        for lembrete in lembretes.vencidos(agora):
            try:
                self._disparar(lembrete, agora)
            except Exception as e:
                log.error("falha ao disparar %s: %s", lembrete.get("id"), telegram_api.redact(e))

    def _disparar(self, lembrete: dict, agora: datetime) -> None:
        atual = lembretes.obter(lembrete["id"])
        if not atual or not atual.get("ativo"):
            return

        proxima = datetime.fromisoformat(lembrete["proxima"])
        atraso_min = (agora - proxima).total_seconds() / 60

        if atraso_min > config.TOLERANCIA_MIN:
            if not lembrete.get("perdido_notificado"):
                telegram_api.send(
                    lembrete["chat_id"],
                    f'Aviso: o lembrete "{lembrete["texto"]}" de '
                    f"{formatar(proxima)} passou enquanto eu estava fora.",
                )
                lembretes.marcar_perdido(lembrete["id"])
                log.warning(
                    "lembrete %s perdido (atraso %.0f min): %r",
                    lembrete["id"],
                    atraso_min,
                    lembrete["texto"],
                )
        else:
            telegram_api.send(lembrete["chat_id"], f"⏰ {lembrete['texto']}")
            lembretes.registrar_envio(lembrete["id"], agora.isoformat(timespec="seconds"))
            log.info("lembrete %s enviado: %r", lembrete["id"], lembrete["texto"])

        if lembrete.get("rrule"):
            nxt = proxima_ocorrencia(lembrete["rrule"], proxima, agora)
            if nxt is None:
                lembretes.concluir(lembrete["id"])
            else:
                lembretes.reagendar(lembrete["id"], nxt.isoformat(timespec="seconds"))
                log.info("lembrete %s reagendado para %s", lembrete["id"], nxt)
        else:
            lembretes.concluir(lembrete["id"])

    def _espera(self) -> float:
        agora = _agora()
        proximas = [
            datetime.fromisoformat(item["proxima"])
            for item in lembretes.ativos()
            if item.get("proxima")
        ]
        if not proximas:
            return 60.0
        delta = (min(proximas) - agora).total_seconds()
        return max(1.0, min(30.0, delta))


def _agora() -> datetime:
    return datetime.now(config.TZ).replace(tzinfo=None) if config.TZ else datetime.now()