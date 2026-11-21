#!/usr/bin/env python3
"""Pipeline OFFLINE: CSV de movimentacoes -> SQLite (`data/analista/analista.db`).

Roda no host, fora do container e fora do boot do bot. O agente apenas abre o
artefato em modo read-only; este modulo nunca e importado pelo runtime.

Somente stdlib (`csv`, `sqlite3`, `hashlib`, `argparse`, `json`), sem pandas.

    python -m analista.ingestao --csv ~/Downloads/relatorio-vendas-consolidado

Garantias:
- escrita atomica: monta `<saida>.tmp`, faz VACUUM e `os.replace` no final, so
  entao o bot ve a base nova (nao precisa reiniciar);
- idempotente: chave primaria `(equipe, id_origem)` + `INSERT OR IGNORE`;
- o artefato se descreve: tabela `meta` com schema, fontes, hashes e totais;
- contrato canonico de 12 colunas (com aliases): formato diferente falha antes
  de escrever qualquer byte.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sqlite3
import sys
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 1
BASE = Path(__file__).resolve().parent.parent
SAIDA_PADRAO = BASE / "data" / "analista" / "analista.db"
CONSOLIDADO_PADRAO = "relatorio_final.csv"
LOTE = 5000

# Coluna canonica -> cabecalhos aceitos (ja normalizados).
ALIASES: dict[str, tuple[str, ...]] = {
    "data": ("data_venda", "data", "dt", "data_mov", "data_movimento"),
    "id_origem": ("id", "id_venda", "codigo"),
    "equipe": ("equipe", "time"),
    "vendedor": ("vendedor", "consultor"),
    "categoria": ("categoria_servico", "categoria"),
    "servico": ("servico", "produto", "descricao", "item"),
    "quantidade": ("quantidade", "qtd", "qtde"),
    "preco_unitario": ("preco_unitario", "valor_unitario", "preco", "valor"),
    "custo_unitario": ("custo", "custo_unitario"),
    "cliente": ("cliente",),
    "estado": ("estado", "uf"),
    "regiao": ("regiao",),
}

COLUNAS = list(ALIASES)

DDL = """
CREATE TABLE vendas (
    equipe         TEXT    NOT NULL,
    id_origem      INTEGER NOT NULL,
    data           TEXT    NOT NULL,   -- ISO YYYY-MM-DD
    vendedor       TEXT,
    categoria      TEXT,
    servico        TEXT,
    quantidade     REAL    NOT NULL,
    preco_unitario REAL    NOT NULL,
    custo_unitario REAL    NOT NULL,
    cliente        TEXT,
    estado         TEXT,
    regiao         TEXT,
    PRIMARY KEY (equipe, id_origem)
);
CREATE INDEX ix_vendas_data      ON vendas(data);
CREATE INDEX ix_vendas_equipe    ON vendas(equipe);
CREATE INDEX ix_vendas_vendedor  ON vendas(vendedor);
CREATE INDEX ix_vendas_servico   ON vendas(servico);
CREATE INDEX ix_vendas_categoria ON vendas(categoria);
CREATE INDEX ix_vendas_cliente   ON vendas(cliente);

CREATE TABLE meta (chave TEXT PRIMARY KEY, valor TEXT);

CREATE VIEW v_vendas AS
SELECT
    equipe, id_origem, data,
    CAST(strftime('%Y', data) AS INTEGER) AS ano,
    strftime('%Y-%m', data)               AS mes,
    CAST(strftime('%m', data) AS INTEGER) AS mes_num,
    CAST(strftime('%d', data) AS INTEGER) AS dia,
    CAST(strftime('%w', data) AS INTEGER) AS dia_semana,
    (CAST(strftime('%m', data) AS INTEGER) - 1) / 3 + 1 AS trimestre,
    vendedor, categoria, servico, cliente, estado, regiao,
    quantidade, preco_unitario, custo_unitario,
    quantidade * preco_unitario                  AS receita,
    quantidade * custo_unitario                  AS custo_total,
    quantidade * (preco_unitario - custo_unitario) AS margem
FROM vendas;

CREATE VIEW v_mensal AS
SELECT
    mes,
    COUNT(*)        AS vendas,
    SUM(quantidade) AS quantidade,
    SUM(receita)    AS receita,
    SUM(custo_total) AS custo,
    SUM(margem)     AS margem
FROM v_vendas
GROUP BY mes;
"""


# ---------------------------------------------------------------------------
# Normalizacao / conversao
# ---------------------------------------------------------------------------

def normalizar_cabecalho(nome: str) -> str:
    """'Data Venda' -> 'data_venda'; remove acentos e pontuacao."""
    s = unicodedata.normalize("NFKD", (nome or "").strip())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.strip().lower().replace(" ", "_").replace("-", "_")
    return "".join(c for c in s if c.isalnum() or c == "_").strip("_")


def resolver_colunas(cabecalho: list[str]) -> dict[str, str]:
    """Canonica -> nome real no CSV. Levanta ValueError listando o que faltou."""
    normalizado = {normalizar_cabecalho(c): c for c in cabecalho if c}
    mapa: dict[str, str] = {}
    faltando: list[str] = []
    for canonica, aliases in ALIASES.items():
        for alias in aliases:
            if alias in normalizado:
                mapa[canonica] = normalizado[alias]
                break
        else:
            faltando.append(canonica)
    if faltando:
        raise ValueError(
            "colunas obrigatorias ausentes: " + ", ".join(faltando)
            + "\ncabecalho lido: " + ", ".join(cabecalho)
        )
    return mapa


def numero(txt) -> float | None:
    """Aceita 1234.56, 1.234,56 e 'R$ 1.234,56'. Virgula presente = formato BR."""
    s = str(txt or "").strip().replace("R$", "").replace(" ", "")
    if not s:
        return None
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def data_iso(txt) -> str | None:
    """Aceita ISO e dd/mm/aaaa (com ou sem hora); devolve YYYY-MM-DD."""
    s = str(txt or "").strip()
    if not s:
        return None
    candidato = s[:10]
    for formato in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(candidato, formato).date().isoformat()
        except ValueError:
            continue
    return None


def brl(valor: float | None) -> str:
    if valor is None:
        return "-"
    return f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def sha256(arquivo: Path) -> str:
    h = hashlib.sha256()
    with arquivo.open("rb") as fh:
        for bloco in iter(lambda: fh.read(1 << 20), b""):
            h.update(bloco)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Leitura
# ---------------------------------------------------------------------------

def resolver_fontes(origem: Path, consolidado: str | None) -> list[Path]:
    """Diretorio -> lista de CSVs. Prefere o consolidado quando ele existe."""
    if origem.is_file():
        return [origem]
    if not origem.is_dir():
        raise SystemExit(f"caminho nao encontrado: {origem}")

    arquivos = sorted(origem.glob("*.csv"))
    if not arquivos:
        raise SystemExit(f"nenhum .csv em {origem}")

    if consolidado:
        alvo = origem / consolidado
        if alvo.exists():
            return [alvo]
    return arquivos


def detectar_dialeto(arquivo: Path) -> csv.Dialect:
    amostra = arquivo.open("r", encoding="utf-8-sig", newline="").read(4096)
    try:
        return csv.Sniffer().sniff(amostra, delimiters=",;\t")
    except csv.Error:
        return csv.excel


def ler_arquivo(arquivo: Path) -> tuple[list[str], csv.DictReader, object]:
    fh = arquivo.open("r", encoding="utf-8-sig", newline="")
    leitor = csv.DictReader(fh, dialect=detectar_dialeto(arquivo))
    return list(leitor.fieldnames or []), leitor, fh


# ---------------------------------------------------------------------------
# Carga
# ---------------------------------------------------------------------------

def gravar(con: sqlite3.Connection, arquivos: list[Path]) -> tuple[Counter, dict]:
    contagem: Counter = Counter()
    fontes: list[dict] = []
    exemplos: list[str] = []

    con.executescript(DDL)
    con.execute("PRAGMA journal_mode=OFF")

    sql = (
        "INSERT OR IGNORE INTO vendas "
        "(equipe, id_origem, data, vendedor, categoria, servico, quantidade, "
        " preco_unitario, custo_unitario, cliente, estado, regiao) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
    )

    for arquivo in arquivos:
        cabecalho, leitor, fh = ler_arquivo(arquivo)
        try:
            mapa = resolver_colunas(cabecalho)
            lote: list[tuple] = []
            linhas = 0
            for i, bruto in enumerate(leitor, start=2):
                linhas += 1
                d = data_iso(bruto.get(mapa["data"]))
                qtd = numero(bruto.get(mapa["quantidade"]))
                preco = numero(bruto.get(mapa["preco_unitario"]))
                custo = numero(bruto.get(mapa["custo_unitario"]))
                ident = numero(bruto.get(mapa["id_origem"]))
                equipe = (bruto.get(mapa["equipe"]) or "").strip()

                if not d or not equipe or ident is None or qtd is None:
                    contagem["invalidas"] += 1
                    if len(exemplos) < 5:
                        exemplos.append(f"{arquivo.name}:{i} data/equipe/id/qtd")
                    continue
                if qtd <= 0 or not preco or preco <= 0 or custo is None or custo < 0:
                    contagem["invalidas"] += 1
                    if len(exemplos) < 5:
                        exemplos.append(f"{arquivo.name}:{i} quantidade/preco/custo")
                    continue

                contagem["lidas"] += 1
                lote.append(
                    (
                        equipe,
                        int(ident),
                        d,
                        (bruto.get(mapa["vendedor"]) or "").strip() or None,
                        (bruto.get(mapa["categoria"]) or "").strip() or None,
                        (bruto.get(mapa["servico"]) or "").strip() or None,
                        qtd,
                        preco,
                        custo,
                        (bruto.get(mapa["cliente"]) or "").strip() or None,
                        (bruto.get(mapa["estado"]) or "").strip() or None,
                        (bruto.get(mapa["regiao"]) or "").strip() or None,
                    )
                )
                if len(lote) >= LOTE:
                    antes = con.total_changes
                    con.executemany(sql, lote)
                    contagem["gravadas"] += con.total_changes - antes
                    lote.clear()
            if lote:
                antes = con.total_changes
                con.executemany(sql, lote)
                contagem["gravadas"] += con.total_changes - antes
        finally:
            fh.close()

        fontes.append(
            {
                "arquivo": arquivo.name,
                "linhas": linhas,
                "sha256": sha256(arquivo),
                "bytes": arquivo.stat().st_size,
            }
        )

    con.commit()
    contagem["duplicadas"] = contagem["lidas"] - contagem["gravadas"]
    return contagem, {"fontes": fontes, "exemplos": exemplos}


def totais(con: sqlite3.Connection) -> dict:
    linha = con.execute(
        "SELECT COUNT(*), MIN(data), MAX(data), SUM(receita), SUM(custo_total), "
        "SUM(margem), COUNT(DISTINCT equipe), COUNT(DISTINCT vendedor), "
        "COUNT(DISTINCT servico), COUNT(DISTINCT cliente) FROM v_vendas"
    ).fetchone()
    return {
        "linhas": linha[0] or 0,
        "data_min": linha[1],
        "data_max": linha[2],
        "receita": linha[3] or 0.0,
        "custo": linha[4] or 0.0,
        "margem": linha[5] or 0.0,
        "equipes": linha[6] or 0,
        "vendedores": linha[7] or 0,
        "servicos": linha[8] or 0,
        "clientes": linha[9] or 0,
    }


def meta_existente(db: Path) -> dict | None:
    if not db.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            return dict(con.execute("SELECT chave, valor FROM meta").fetchall())
        finally:
            con.close()
    except sqlite3.Error:
        return None


def gerar(destino: Path, arquivos: list[Path], forcar: bool = False) -> int:
    destino = destino.resolve()
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.with_suffix(destino.suffix + ".tmp")

    hashes = {a.name: sha256(a) for a in arquivos}
    anterior = meta_existente(destino)
    if anterior and not forcar:
        try:
            gravado = json.loads(anterior.get("fontes") or "[]")
        except json.JSONDecodeError:
            gravado = []
        if {f["arquivo"]: f["sha256"] for f in gravado} == hashes:
            print(f"sem mudancas: {destino} ja reflete {len(arquivos)} arquivo(s)")
            print("use --forcar para regerar mesmo assim")
            return 0

    if tmp.exists():
        tmp.unlink()

    con = sqlite3.connect(tmp)
    try:
        try:
            contagem, extra = gravar(con, arquivos)
        except BaseException:
            # validacao falhou: nao deixa .tmp para tras nem toca no artefato bom
            con.close()
            tmp.unlink(missing_ok=True)
            raise
        info = totais(con)

        metas = {
            "schema_version": str(SCHEMA_VERSION),
            "gerado_em": datetime.now().isoformat(timespec="seconds"),
            "fontes": json.dumps(extra["fontes"], ensure_ascii=False),
            "linhas": str(info["linhas"]),
            "data_min": info["data_min"] or "",
            "data_max": info["data_max"] or "",
            "receita_total": f"{info['receita']:.2f}",
            "custo_total": f"{info['custo']:.2f}",
            "margem_total": f"{info['margem']:.2f}",
        }
        con.executemany("INSERT OR REPLACE INTO meta (chave, valor) VALUES (?,?)", metas.items())
        con.commit()
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute("VACUUM")
        con.execute("PRAGMA optimize")
    finally:
        con.close()

    os.replace(tmp, destino)
    os.chmod(destino, 0o644)  # o container roda como 1000:1000

    print(f"fontes: {len(arquivos)} arquivo(s)")
    for f in extra["fontes"]:
        print(f"  {f['arquivo']:32s} {f['linhas']:>7} linhas  {f['sha256'][:12]}")
    print(
        f"gravadas {contagem['gravadas']} | duplicadas {contagem['duplicadas']} "
        f"| invalidas {contagem['invalidas']}"
    )
    for exemplo in extra["exemplos"]:
        print(f"  ignorada: {exemplo}")
    print(
        f"periodo {info['data_min']} .. {info['data_max']} | "
        f"{info['equipes']} equipes | {info['vendedores']} vendedores | "
        f"{info['servicos']} servicos | {info['clientes']} clientes"
    )
    margem_pct = (info["margem"] / info["receita"] * 100) if info["receita"] else 0
    print(
        f"receita {brl(info['receita'])} | custo {brl(info['custo'])} | "
        f"margem {brl(info['margem'])} ({margem_pct:.2f}%)"
    )
    tamanho = destino.stat().st_size / (1024 * 1024)
    print(f"db: {destino} ({tamanho:.1f} MB) sha256 {sha256(destino)[:12]}")
    return 0


def inspecionar(db: Path) -> int:
    meta = meta_existente(db)
    if meta is None:
        print(f"nao encontrei um artefato valido em {db}")
        return 1
    versao = meta.get("schema_version")
    print(f"db: {db}")
    print(f"schema_version: {versao} (suportado: {SCHEMA_VERSION})")
    if versao != str(SCHEMA_VERSION):
        print("ATENCAO: versao diferente; regenere com --forcar")
    print(f"gerado_em: {meta.get('gerado_em')}")
    print(
        f"linhas {meta.get('linhas')} | periodo {meta.get('data_min')} .. "
        f"{meta.get('data_max')}"
    )
    print(
        f"receita {brl(float(meta.get('receita_total') or 0))} | "
        f"custo {brl(float(meta.get('custo_total') or 0))} | "
        f"margem {brl(float(meta.get('margem_total') or 0))}"
    )
    for fonte in json.loads(meta.get("fontes") or "[]"):
        print(f"  {fonte['arquivo']:32s} {fonte['linhas']:>7} linhas")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m analista.ingestao",
        description="Gera o SQLite do analista a partir dos CSVs (offline).",
    )
    parser.add_argument("--csv", type=Path, default=None,
                        help="arquivo ou diretorio dos CSVs")
    parser.add_argument("--saida", type=Path, default=None,
                        help=f"artefato .db (default: {SAIDA_PADRAO})")
    parser.add_argument("--consolidado", default=CONSOLIDADO_PADRAO,
                        help="quando o diretorio tem este arquivo, usa so ele "
                             f"(default: {CONSOLIDADO_PADRAO})")
    parser.add_argument("--forcar", action="store_true", help="regera mesmo sem mudancas")
    parser.add_argument("--inspecionar", type=Path, default=None,
                        help="imprime o meta de um artefato existente e sai")
    args = parser.parse_args(argv)

    if args.inspecionar:
        return inspecionar(args.inspecionar)

    origem = args.csv or (
        Path(os.environ["ANALISTA_CSV"]) if os.environ.get("ANALISTA_CSV") else None
    )
    if not origem:
        parser.error("informe --csv <arquivo|diretorio> (ou exporte ANALISTA_CSV)")

    destino = args.saida or (
        Path(os.environ["ANALISTA_DB"]) if os.environ.get("ANALISTA_DB") else SAIDA_PADRAO
    )

    arquivos = resolver_fontes(origem, args.consolidado)
    try:
        return gerar(destino, arquivos, forcar=args.forcar)
    except ValueError as e:
        print(f"erro: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())