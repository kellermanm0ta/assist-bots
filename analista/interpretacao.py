#!/usr/bin/env python3
"""Pergunta em texto livre -> tool (contrato JSON) -> resultado do SQLite.

O LLM escolhe *qual* tool e com *quais* argumentos; ele nunca calcula nem
devolve numero. Se a pergunta nao couber no catalogo, ele pode propor um SELECT
(escape hatch) que passa pelos guardrails de `analista.db`.

Contrato de saida (uma chamada, JSON puro):

    {"acao": "ferramenta", "ferramenta": "ranking",
     "args": {"dimensao": "vendedor", "periodo": "2024", "top_n": 5,
              "filtros": {"equipe": "Equipe Vega"}}}

    {"acao": "sql", "sql": "select ... from v_vendas where ..."}
    {"acao": "texto", "resposta": "..."}

Falha de tool/SQL volta ao modelo uma vez com a mensagem de erro; na segunda,
o bot responde com um pedido de esclarecimento em vez de insistir.
"""
from __future__ import annotations

import json
import re

from analista import conversa, db, ferramentas
from analista.resultado import Resultado
from common import llm, logsetup

log = logsetup.get_logger("interpretacao")

_ESQUEMA = """Views disponiveis (SQLite, somente leitura):
- vendas(equipe, id_origem, data ISO, vendedor, categoria, servico, quantidade,
  preco_unitario, custo_unitario, cliente, estado, regiao)
- v_vendas(vendas + receita = quantidade*preco_unitario,
  custo_total = quantidade*custo_unitario,
  margem = quantidade*(preco_unitario-custo_unitario),
  ano, mes 'YYYY-MM', mes_num, dia, dia_semana (0=domingo), trimestre)
- v_mensal(mes, vendas, quantidade, receita, custo, margem)"""

_REGRAS = """Regras:
- Responda APENAS um objeto JSON, sem markdown e sem texto fora dele.
- Se a pergunta pede um dado, escolha UMA ferramenta e preencha `args`.
- `periodo` e texto em portugues: "2024", "marco de 2024",
  "1o trimestre de 2023", "ultimos 30 dias", "de 01/03 a 15/04", "tudo".
- Sem periodo explicito, use "tudo".
- `filtros` e um objeto com chaves de dimensao (equipe, vendedor, servico,
  categoria, cliente, estado, regiao, ano). Ex: {"equipe": "Equipe Vega"}.
- Se faltar informacao para decidir, acao "texto" pedindo o esclarecimento.
- NUNCA calcule ou invente numeros: quem soma e o SQLite.
- Use "sql" so quando o catalogo nao resolver (ex: cruzamento com condicao
  sobre margem por cliente).
- Se a pergunta for de outro assunto, acao "texto" dizendo que so analisa a base."""


def _sistema() -> str:
    nome = llm.bot_name()
    persona = llm.load_agent("analista")["prompt"]
    base = ferramentas.base()
    faixa = f"{base[0]} a {base[1]}" if base else "desconhecido"
    cabeca = persona + "\n\n" if persona else ""
    return (
        f"{cabeca}Voce responde perguntas sobre a base de vendas de servicos "
        f"(periodo {faixa}).\n\n"
        f"Ferramentas disponiveis:\n{ferramentas.catalogo_para_llm()}\n\n"
        f"{_ESQUEMA}\n\n{_REGRAS}\n\n"
        'Formato: {"acao": "ferramenta", "ferramenta": "...", "args": {...}}\n'
        'ou      {"acao": "sql", "sql": "..."}\n'
        'ou      {"acao": "texto", "resposta": "..."}'
    )


class Resposta:
    """Resultado de uma pergunta: ou uma tabela, ou so texto."""

    def __init__(
        self,
        resultado: Resultado | None = None,
        texto: str | None = None,
        ferramenta: str = "",
        args: dict | None = None,
    ) -> None:
        self.resultado = resultado
        self.texto = texto
        self.ferramenta = ferramenta
        self.args = args or {}


def _resultado_sql(colunas: list[str], linhas: list[dict], sql: str) -> Resultado:
    return Resultado(
        titulo="Consulta livre",
        colunas=[(c, c) for c in colunas],
        linhas=linhas,
        metadados={"sql": sql, "linhas": len(linhas)},
        grafico=_grafico_inferido(colunas, linhas),
    )


def _grafico_inferido(colunas: list[str], linhas: list[dict]) -> dict | None:
    """Chuta linha/barra para resultado de SQL livre (sem prometer nada)."""
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


def responder(pergunta: str, chat_id: int | None = None) -> Resposta:
    """Resolve a pergunta com o LLM. Sem chave, devolve texto explicativo."""
    if not llm.chave():
        return Resposta(
            texto=(
                "Estou sem OPENROUTER_API_KEY, entao nao consigo interpretar "
                "pergunta em texto livre.\n\nUse os comandos diretos:\n"
                "/resumo 2024\n/ranking vendedor 2024\n/serie receita 2023\n"
                "/tabela equipe 2024\n/base"
            )
        )

    historico = conversa.como_texto(chat_id) if chat_id is not None else ""
    entrada = f"Pergunta: {pergunta}"
    if historico:
        entrada = f"Historico recente:\n{historico}\n\n{entrada}"

    mensagens = [
        {"role": "system", "content": _sistema()},
        {"role": "user", "content": entrada},
    ]

    erro = ""
    for tentativa in (1, 2):
        payload = {
            "model": llm.model_for("analista"),
            "temperature": 0,
            "messages": mensagens,
            "response_format": {"type": "json_object"},
        }
        obj = llm.chamar_json(payload, descricao=f"analista tentativa {tentativa}")
        if obj is None:
            return Resposta(texto="Nao consegui consultar o modelo agora. Tente de novo.")

        acao = str(obj.get("acao") or "").strip().lower()
        log.info("decisao LLM: acao=%s tool=%s", acao, obj.get("ferramenta"))

        if acao == "texto":
            texto = str(obj.get("resposta") or "").strip()
            return Resposta(texto=texto or "Nao entendi. Pode reformular?")

        if acao == "ferramenta":
            nome = str(obj.get("ferramenta") or "").strip()
            args = obj.get("args") or {}
            if not isinstance(args, dict):
                args = {}
            try:
                resultado = ferramentas.executar(nome, args)
            except ferramentas.ErroFerramenta as e:
                erro = str(e)
                log.warning("tool %s falhou: %s", nome, erro)
            else:
                if chat_id is not None:
                    conversa.registrar(chat_id, pergunta, nome, args)
                return Resposta(resultado=resultado, ferramenta=nome, args=args)

        elif acao == "sql":
            sql = str(obj.get("sql") or "").strip()
            try:
                colunas, linhas, final = db.executar_sql(sql)
            except (db.ErroSQL, db.ErroConsulta) as e:
                erro = str(e)
                log.warning("sql recusado/falhou: %s", erro)
            else:
                if chat_id is not None:
                    conversa.registrar(chat_id, pergunta, "consultar_sql", {"sql": sql})
                return Resposta(
                    resultado=_resultado_sql(colunas, linhas, final),
                    ferramenta="consultar_sql",
                    args={"sql": sql},
                )
        else:
            erro = f'acao desconhecida: "{acao}"'

        mensagens.append({"role": "assistant", "content": json.dumps(obj, ensure_ascii=False)})
        mensagens.append(
            {
                "role": "user",
                "content": (
                    f"A execucao falhou: {erro}\n"
                    "Corrija o JSON. Se nao der para resolver, use acao \"texto\"."
                ),
            }
        )

    return Resposta(texto=f"Nao consegui resolver essa pergunta ({erro}). Pode detalhar?")