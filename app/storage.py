"""Persistência em SQLite (stdlib, sem ORM).

Guarda os tokens do seller e o histórico de publicações. O volume aqui é baixo
(um seller, alguns milhares de anúncios), então SQLite dá conta com folga e
evita subir um Postgres só para isso no VPS.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_settings
from app.ml.oauth import TokenBundle

_SCHEMA = """
CREATE TABLE IF NOT EXISTS contas_ml (
    user_id       INTEGER PRIMARY KEY,
    nickname      TEXT,
    access_token  TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    scope         TEXT,
    atualizado_em TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lotes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    arquivo       TEXT NOT NULL,
    total_linhas  INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'criado',
    dry_run       INTEGER NOT NULL DEFAULT 1,
    mapeamento    TEXT,
    criado_em     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS itens (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lote_id     INTEGER NOT NULL REFERENCES lotes(id),
    linha       INTEGER NOT NULL,
    sku         TEXT,
    titulo      TEXT,
    payload     TEXT,
    status      TEXT NOT NULL DEFAULT 'pendente',
    ml_item_id  TEXT,
    permalink   TEXT,
    erro        TEXT,
    atualizado_em TEXT
);

CREATE INDEX IF NOT EXISTS idx_itens_lote ON itens(lote_id);
-- Impede republicar o mesmo SKU por engano em execuções repetidas.
CREATE UNIQUE INDEX IF NOT EXISTS idx_itens_sku_publicado
    ON itens(sku) WHERE status = 'publicado' AND sku IS NOT NULL;
"""

# Colunas acrescentadas depois da primeira versão. Como o banco vive num volume
# que sobrevive aos deploys, elas precisam ser aplicadas em bancos já existentes.
_MIGRACOES = {
    "itens": {
        "descricao_erp":      "TEXT",
        "marca":              "TEXT",
        "quantidade":         "INTEGER",
        "preco":              "REAL",
        "confianca":          "TEXT",
        "catalog_product_id": "TEXT",
        "catalog_nome":       "TEXT",
        "catalog_foto":       "TEXT",
        "catalog_permalink":  "TEXT",
        "catalog_category_id": "TEXT",
        "catalog_atributos":  "TEXT",
        "aprovado":           "INTEGER NOT NULL DEFAULT 0",
        "valor":              "REAL NOT NULL DEFAULT 0",
        "qualidade_codigo":   "TEXT",
        "aplicacao":          "TEXT",
        "decidido_em":        "TEXT",
        "adiado_vezes":       "INTEGER DEFAULT 0",
        "loja_url":           "TEXT",
        "loja_nome":          "TEXT",
        "loja_sku":           "TEXT",
        "loja_preco":         "REAL",
        "loja_fotos":         "TEXT",
        "loja_descricao":     "TEXT",
    },
}


def _migrar(conn) -> None:
    for tabela, colunas in _MIGRACOES.items():
        existentes = {r["name"] for r in
                      conn.execute(f"PRAGMA table_info({tabela})").fetchall()}
        for nome, tipo in colunas.items():
            if nome not in existentes:
                conn.execute(f"ALTER TABLE {tabela} ADD COLUMN {nome} {tipo}")


def _caminho_db() -> str:
    s = get_settings()
    url = s.database_url
    caminho = url.replace("sqlite:///", "") if url.startswith("sqlite:///") else url
    Path(caminho).parent.mkdir(parents=True, exist_ok=True)
    return caminho


@contextmanager
def conexao():
    conn = sqlite3.connect(_caminho_db(), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    os.makedirs(get_settings().data_dir, exist_ok=True)
    with conexao() as conn:
        conn.executescript(_SCHEMA)
        _migrar(conn)


# --------------------------------------------------------------------------
# Tokens
# --------------------------------------------------------------------------

def salvar_token(bundle: TokenBundle, nickname: str | None = None) -> None:
    agora = datetime.now(timezone.utc).isoformat()
    with conexao() as conn:
        conn.execute(
            """
            INSERT INTO contas_ml
                (user_id, nickname, access_token, refresh_token, expires_at, scope, atualizado_em)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                access_token  = excluded.access_token,
                refresh_token = excluded.refresh_token,
                expires_at    = excluded.expires_at,
                scope         = excluded.scope,
                nickname      = COALESCE(excluded.nickname, contas_ml.nickname),
                atualizado_em = excluded.atualizado_em
            """,
            (
                bundle.user_id,
                nickname,
                bundle.access_token,
                bundle.refresh_token,
                bundle.expires_at.isoformat(),
                bundle.scope,
                agora,
            ),
        )


def carregar_token() -> tuple[TokenBundle, str | None] | None:
    """Retorna a conta conectada mais recente, ou None se nenhuma."""
    with conexao() as conn:
        row = conn.execute(
            "SELECT * FROM contas_ml ORDER BY atualizado_em DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    bundle = TokenBundle(
        access_token=row["access_token"],
        refresh_token=row["refresh_token"],
        user_id=row["user_id"],
        expires_at=datetime.fromisoformat(row["expires_at"]),
        scope=row["scope"] or "",
    )
    return bundle, row["nickname"]


def desconectar() -> None:
    with conexao() as conn:
        conn.execute("DELETE FROM contas_ml")


# --------------------------------------------------------------------------
# Lotes e itens
# --------------------------------------------------------------------------

def criar_lote(arquivo: str, total_linhas: int, dry_run: bool, mapeamento: dict) -> int:
    with conexao() as conn:
        cur = conn.execute(
            """INSERT INTO lotes (arquivo, total_linhas, dry_run, mapeamento, criado_em)
               VALUES (?, ?, ?, ?, ?)""",
            (
                arquivo,
                total_linhas,
                1 if dry_run else 0,
                json.dumps(mapeamento, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return int(cur.lastrowid)


def registrar_item(lote_id: int, linha: int, sku: str | None, titulo: str | None,
                   payload: dict, **extras) -> int:
    """Registra o item do lote.

    `extras` aceita: descricao_erp, marca, quantidade, preco, confianca,
    catalog_product_id, catalog_nome, catalog_foto, catalog_permalink,
    catalog_atributos, status, erro.
    """
    campos = {
        "lote_id": lote_id,
        "linha": linha,
        "sku": sku,
        "titulo": titulo,
        "payload": json.dumps(payload, ensure_ascii=False),
        "atualizado_em": datetime.now(timezone.utc).isoformat(),
    }
    permitidos = {"descricao_erp", "marca", "quantidade", "preco", "confianca",
                  "catalog_product_id", "catalog_nome", "catalog_foto",
                  "catalog_permalink", "catalog_atributos", "catalog_category_id",
                  "status", "erro", "valor", "qualidade_codigo", "aplicacao",
                  "loja_url", "loja_nome", "loja_sku", "loja_preco",
                  "loja_fotos", "loja_descricao"}
    for chave, valor in extras.items():
        if chave in permitidos and valor is not None:
            campos[chave] = (json.dumps(valor, ensure_ascii=False)
                             if isinstance(valor, (dict, list)) else valor)

    colunas = ", ".join(campos)
    marcas = ", ".join("?" for _ in campos)
    with conexao() as conn:
        cur = conn.execute(
            f"INSERT INTO itens ({colunas}) VALUES ({marcas})",
            tuple(campos.values()),
        )
        return int(cur.lastrowid)


def adiar_item(item_id: int) -> None:
    """Manda o item para o fim da fila sem perder o valor original.

    Conta quantas vezes já foi pulado: uma peça pulada três vezes é sinal de
    que falta informação, não de que o operador está enrolando.
    """
    with conexao() as conn:
        conn.execute(
            """UPDATE itens
               SET valor = -ABS(valor),
                   adiado_vezes = COALESCE(adiado_vezes, 0) + 1,
                   atualizado_em = ?
               WHERE id = ?""",
            (datetime.now(timezone.utc).isoformat(), item_id))


def itens_por_status(lote_id: int, status: str) -> list[sqlite3.Row]:
    with conexao() as conn:
        return conn.execute(
            "SELECT * FROM itens WHERE lote_id = ? AND status = ? ORDER BY valor DESC",
            (lote_id, status),
        ).fetchall()


def proximo_da_fila(lote_id: int, status: str = "na_fila") -> sqlite3.Row | None:
    """Devolve o próximo item a trabalhar: o de MAIOR VALOR ainda pendente."""
    with conexao() as conn:
        return conn.execute(
            """SELECT * FROM itens
               WHERE lote_id = ? AND status = ?
               ORDER BY valor DESC, linha ASC LIMIT 1""",
            (lote_id, status),
        ).fetchone()


#: Status que significam "esta peça ainda não foi trabalhada".
#: Tudo o que NÃO está aqui conta como decidido. Foi assim que o contador
#: quebrou na v0.11.0: a tela somava uma lista fixa de status ('vinculado',
#: que nem existe) e, a cada status novo que eu criava, o número parava mais.
PENDENTES = ("na_fila",)

#: Status de itens que a triagem descartou antes da fila começar (código
#: inutilizável, sem código, preço inválido). Nunca foram trabalhados por
#: ninguém, então não entram nem como feitos nem como pendentes — senão a
#: barra já nasce com 353 de 1687 "decididas", que é falso.
FORA_DA_FILA = ("sem_dado",)

#: Status em que a peça tem tudo que o ML precisa e só falta mandar.
PRONTOS_PARA_PUBLICAR = ("pronto_com_fotos", "foto_do_operador",
                         "aguardando_aprovacao")


def _carimbar_decisao(conn, item_id: int, status: str, quando: str) -> None:
    """Grava a hora da decisão na primeira vez que o item sai de 'na_fila'.

    Fica aqui, num lugar só, em vez de em cada rota: era fácil demais eu
    esquecer de carimbar num caminho novo e o histórico ficar furado.
    """
    if status in PENDENTES:
        return
    conn.execute(
        "UPDATE itens SET decidido_em = ? WHERE id = ? AND decidido_em IS NULL",
        (quando, item_id))


def progresso_da_fila(lote_id: int) -> dict:
    """Conta o andamento do lote, por status e no total.

    Devolve, além do detalhamento por status, as chaves ``_total``,
    ``_feitos``, ``_faltam`` e ``_adiados`` — que é o que a tela mostra.
    """
    with conexao() as conn:
        linhas = conn.execute(
            """SELECT status, COUNT(*) n, COALESCE(SUM(valor),0) v
               FROM itens WHERE lote_id = ? GROUP BY status""",
            (lote_id,),
        ).fetchall()
        adiados = conn.execute(
            """SELECT COUNT(*) n FROM itens
               WHERE lote_id = ? AND status = 'na_fila' AND valor < 0""",
            (lote_id,),
        ).fetchone()["n"]
        ultima = conn.execute(
            """SELECT MAX(decidido_em) d FROM itens WHERE lote_id = ?""",
            (lote_id,),
        ).fetchone()["d"]

    resumo = {r["status"]: {"itens": r["n"], "valor": r["v"]} for r in linhas}

    def _soma(chaves) -> int:
        return sum(resumo.get(k, {}).get("itens", 0) for k in chaves)

    fora = _soma(FORA_DA_FILA)
    total = sum(v["itens"] for v in resumo.values()) - fora
    faltam = _soma(PENDENTES)

    resumo["_total"] = total
    resumo["_feitos"] = total - faltam
    resumo["_faltam"] = faltam
    resumo["_fora_da_fila"] = fora
    resumo["_prontos"] = _soma(PRONTOS_PARA_PUBLICAR)
    resumo["_publicados"] = _soma(("publicado",))
    resumo["_adiados"] = adiados
    resumo["_ultima_decisao"] = ultima
    return resumo


def itens_prontos_para_publicar(lote_id: int) -> list[sqlite3.Row]:
    """As peças que já têm foto e dados — só falta mandar para o ML."""
    marcas = ",".join("?" for _ in PRONTOS_PARA_PUBLICAR)
    with conexao() as conn:
        return conn.execute(
            f"""SELECT * FROM itens
                WHERE lote_id = ? AND status IN ({marcas})
                ORDER BY valor DESC""",
            (lote_id, *PRONTOS_PARA_PUBLICAR),
        ).fetchall()


def atualizar_dados_catalogo(item_id: int, **campos) -> None:
    """Atualiza as colunas de catálogo de um item (usado no vínculo manual)."""
    permitidos = {"status", "confianca", "catalog_product_id", "catalog_nome",
                  "catalog_foto", "catalog_permalink", "catalog_category_id",
                  "catalog_atributos", "erro", "preco", "decidido_em",
                  "loja_url", "loja_nome", "loja_sku", "loja_preco",
                  "loja_fotos", "loja_descricao"}
    campos = {k: v for k, v in campos.items() if k in permitidos}
    if not campos:
        return
    agora = datetime.now(timezone.utc).isoformat()
    campos["atualizado_em"] = agora
    sets = ", ".join(f"{k} = ?" for k in campos)
    with conexao() as conn:
        conn.execute(f"UPDATE itens SET {sets} WHERE id = ?",
                     (*campos.values(), item_id))
        if campos.get("status"):
            _carimbar_decisao(conn, item_id, campos["status"], agora)


def item(item_id: int) -> sqlite3.Row | None:
    with conexao() as conn:
        return conn.execute("SELECT * FROM itens WHERE id = ?",
                            (item_id,)).fetchone()


def definir_aprovacao(lote_id: int, ids_aprovados: list[int]) -> int:
    """Marca como aprovados apenas os IDs informados. O resto fica reprovado."""
    with conexao() as conn:
        conn.execute("UPDATE itens SET aprovado = 0 WHERE lote_id = ?", (lote_id,))
        if not ids_aprovados:
            return 0
        marcas = ",".join("?" for _ in ids_aprovados)
        cur = conn.execute(
            f"""UPDATE itens SET aprovado = 1
                WHERE lote_id = ? AND id IN ({marcas})
                  AND status = 'aguardando_aprovacao'""",
            (lote_id, *ids_aprovados),
        )
        return cur.rowcount


def itens_aprovados(lote_id: int) -> list[sqlite3.Row]:
    with conexao() as conn:
        return conn.execute(
            """SELECT * FROM itens
               WHERE lote_id = ? AND aprovado = 1 AND status = 'aguardando_aprovacao'
               ORDER BY linha""",
            (lote_id,),
        ).fetchall()


def atualizar_item(item_id: int, *, status: str, ml_item_id: str | None = None,
                   permalink: str | None = None, erro: str | None = None) -> None:
    agora = datetime.now(timezone.utc).isoformat()
    with conexao() as conn:
        conn.execute(
            """UPDATE itens
               SET status = ?, ml_item_id = ?, permalink = ?, erro = ?, atualizado_em = ?
               WHERE id = ?""",
            (status, ml_item_id, permalink, erro, agora, item_id),
        )
        _carimbar_decisao(conn, item_id, status, agora)


def itens_do_lote(lote_id: int) -> list[sqlite3.Row]:
    with conexao() as conn:
        return conn.execute(
            "SELECT * FROM itens WHERE lote_id = ? ORDER BY linha", (lote_id,)
        ).fetchall()


def lote(lote_id: int) -> sqlite3.Row | None:
    with conexao() as conn:
        return conn.execute("SELECT * FROM lotes WHERE id = ?", (lote_id,)).fetchone()


def listar_lotes(limite: int = 30) -> list[sqlite3.Row]:
    with conexao() as conn:
        return conn.execute(
            "SELECT * FROM lotes ORDER BY id DESC LIMIT ?", (limite,)
        ).fetchall()


def atualizar_status_lote(lote_id: int, status: str) -> None:
    with conexao() as conn:
        conn.execute("UPDATE lotes SET status = ? WHERE id = ?", (status, lote_id))


def sku_ja_publicado(sku: str) -> str | None:
    """Retorna o ml_item_id se o SKU já foi publicado com sucesso antes."""
    if not sku:
        return None
    with conexao() as conn:
        row = conn.execute(
            "SELECT ml_item_id FROM itens WHERE sku = ? AND status = 'publicado' LIMIT 1",
            (sku,),
        ).fetchone()
    return row["ml_item_id"] if row else None


def diagnostico_persistencia() -> dict:
    """Diz se o banco está num volume que sobrevive ao deploy.

    O trabalho do operador — 1.333 decisões — vive nesse arquivo. Se a pasta
    ``data/`` não for um volume montado, o EasyPanel joga tudo fora no próximo
    deploy e a fila recomeça do zero. Já perdemos o token do Mercado Livre
    assim uma vez; melhor a tela avisar do que descobrir depois.
    """
    caminho = Path(_caminho_db())
    pasta = caminho.parent
    montado = False
    try:
        montado = os.path.ismount(str(pasta))
    except OSError:
        pass

    decididas = 0
    if caminho.exists():
        try:
            with conexao() as conn:
                decididas = conn.execute(
                    "SELECT COUNT(*) n FROM itens WHERE decidido_em IS NOT NULL"
                ).fetchone()["n"]
        except sqlite3.Error:
            pass

    return {
        "caminho": str(caminho),
        "pasta": str(pasta),
        "volume_montado": montado,
        "existe": caminho.exists(),
        "tamanho_bytes": caminho.stat().st_size if caminho.exists() else 0,
        "decisoes_gravadas": decididas,
    }
