#!/usr/bin/env python3
"""Mede a cobertura do catálogo do Mercado Livre para a planilha do ERP.

Este é o teste que decide o projeto: descobre quantos dos seus itens já existem
no catálogo do ML e portanto podem ser publicados SEM foto própria.

Uso:
    python scripts/testar_catalogo.py planilha.xls --amostra 100
    python scripts/testar_catalogo.py planilha.xls --aba "Acima de R$1000,00"

Requer uma conta do ML já conectada pela interface web (o token fica no SQLite).
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import random
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import storage                                    # noqa: E402
from app.ml.catalog import buscar_no_catalogo              # noqa: E402
from app.ml.client import MLClient, MLApiError             # noqa: E402
from app.sheets import ler_planilha                        # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("planilha")
    ap.add_argument("--aba", action="append", dest="abas")
    ap.add_argument("--amostra", type=int, default=100,
                    help="quantos itens sortear (0 = todos)")
    ap.add_argument("--saida", default="cobertura_catalogo.csv")
    ap.add_argument("--concorrencia", type=int, default=3)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    storage.init_db()
    if not storage.carregar_token():
        print("ERRO: nenhuma conta do Mercado Livre conectada.\n"
              "      Abra a interface web e clique em 'Conectar conta'.", file=sys.stderr)
        return 1

    itens = ler_planilha(args.planilha, args.abas)
    if args.amostra and args.amostra < len(itens):
        random.seed(args.seed)          # amostra reproduzível
        itens = random.sample(itens, args.amostra)

    print(f"Consultando o catálogo para {len(itens)} itens "
          f"(concorrência {args.concorrencia})...\n")

    client = MLClient()
    sem = asyncio.Semaphore(args.concorrencia)
    linhas = []
    contagem: Counter[str] = Counter()

    async def checar(p):
        async with sem:
            try:
                cands = await buscar_no_catalogo(client, p.codigo, p.descricao)
            except MLApiError as exc:
                contagem["erro"] += 1
                return {"codigo": p.codigo, "descricao": p.descricao,
                        "confianca": "erro", "produto": "", "nome": "",
                        "detalhe": exc.mensagem_amigavel()[:120]}

            if not cands:
                contagem["sem_match"] += 1
                return {"codigo": p.codigo, "descricao": p.descricao,
                        "confianca": "sem_match", "produto": "", "nome": "",
                        "detalhe": ""}

            melhor = cands[0]
            contagem[melhor.confianca] += 1
            return {"codigo": p.codigo, "descricao": p.descricao,
                    "confianca": melhor.confianca,
                    "produto": melhor.catalog_product_id,
                    "nome": melhor.nome[:80], "detalhe": melhor.motivo}

    linhas = await asyncio.gather(*(checar(p) for p in itens))

    total = len(linhas)
    alta = contagem["alta"]
    print("=" * 62)
    print(f"{'RESULTADO':<28}{'ITENS':>10}{'%':>10}")
    print("-" * 62)
    for chave, rotulo in [("alta", "match confiável (publica s/ foto)"),
                          ("media", "match provável (revisar)"),
                          ("baixa", "match fraco (revisar)"),
                          ("sem_match", "sem catálogo (precisa de foto)"),
                          ("erro", "erro de consulta")]:
        n = contagem[chave]
        if n:
            print(f"{rotulo:<28}{n:>10}{n/total*100:>9.1f}%")
    print("=" * 62)
    print(f"\nPublicáveis hoje sem foto: {alta} de {total} ({alta/total*100:.1f}%)")
    if total < 300:
        print("Obs.: amostra pequena — a margem de erro é alta. Rode com "
              "--amostra 0 para o número definitivo.")

    with open(args.saida, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(linhas[0].keys()))
        w.writeheader()
        w.writerows(linhas)
    print(f"\nDetalhe item a item salvo em: {args.saida}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
