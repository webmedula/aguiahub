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
                   payload: dict) -> int:
    with conexao() as conn:
        cur = conn.execute(
            """INSERT INTO itens (lote_id, linha, sku, titulo, payload, atualizado_em)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                lote_id,
                linha,
                sku,
                titulo,
                json.dumps(payload, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return int(cur.lastrowid)


def atualizar_item(item_id: int, *, status: str, ml_item_id: str | None = None,
                   permalink: str | None = None, erro: str | None = None) -> None:
    with conexao() as conn:
        conn.execute(
            """UPDATE itens
               SET status = ?, ml_item_id = ?, permalink = ?, erro = ?, atualizado_em = ?
               WHERE id = ?""",
            (status, ml_item_id, permalink, erro,
             datetime.now(timezone.utc).isoformat(), item_id),
        )


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
