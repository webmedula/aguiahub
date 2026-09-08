"""Medir de onde viria a foto de cada peça da fila — sem mudar nada.

Por que existe: em 08/09/2026 a fila tinha 1.682 peças paradas e 5 anúncios
publicados. A conversa travou numa pergunta que ninguém sabia responder:
**quantas dessas peças conseguiriam virar anúncio sem alguém tirar foto?**

Um anúncio no Mercado Livre precisa de cinco coisas. Quatro a planilha dá de
graça (título, preço, quantidade, código) e a quinta o ML adivinha
(categoria). Sobra a foto — e é só nela que as 1.682 estão presas. Então a
pergunta que decide o próximo mês de trabalho é de onde a foto viria:

  1. **catálogo do ML** — o anúncio herda foto, título e ficha do produto de
     catálogo. Custo de foto: zero. Nunca foi medido neste projeto, desde a
     v0.1: era a pendência mais antiga em aberto.
  2. **loja da Águia** — foto própria, sem questão de licença.
  3. **fonte autorizada** — site de marca representada, medido em /fontes.
  4. **câmera do estoque** — o resto. Ilimitado, mas custa tempo de gente.

Esta medição responde isso numa amostra e projeta para a fila inteira.

O QUE ELA NÃO FAZ
-----------------

**Não escreve nada.** Nenhum status muda, nenhuma peça fica "pronta", nada é
publicado. É diagnóstico: serve para decidir onde investir esforço, e uma
medição que altera o que está medindo não serve para decidir coisa nenhuma.
Há teste lendo o código-fonte deste módulo para garantir isso.

**Não consulta a fila inteira.** 1.682 peças × 2 consultas ao catálogo seriam
milhares de requisições ao Mercado Livre para responder uma pergunta que uma
amostra responde. A amostra é estratificada por faixa de valor, porque a
resposta para a peça de R$ 10 mil não é a mesma da peça de R$ 35 — e é
justamente essa diferença que decide o que vale a pena anunciar.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import datetime, timezone

from app import storage
from app.ml.catalog import buscar_no_catalogo, escolher_publicavel
from app.ml.client import MLClient, MLApiError

log = logging.getLogger("aguiahub.cobertura")

#: Quantas peças consultar. 150 numa fila de 1.682 dá margem de erro de ~7
#: pontos percentuais — suficiente para distinguir "3%" de "30%", que é a
#: decisão em jogo. Mais que isso vira requisição a mais sem mudar a decisão.
AMOSTRA_PADRAO = 150

#: Consultas simultâneas ao Mercado Livre. Baixo de propósito: é a conta real
#: da Águia, e tomar 429 no meio de uma medição inutiliza a medição.
CONCORRENCIA = 5

#: Faixas de valor parado. Existem porque a decisão de anunciar é diferente em
#: cada uma: a peça de R$ 35 pode não pagar a comissão do ML e o frete.
FAIXAS = (
    ("alto",  1000.0, float("inf"), "acima de R$ 1.000"),
    ("medio",  100.0, 1000.0,       "de R$ 100 a R$ 1.000"),
    ("baixo",    0.0, 100.0,        "abaixo de R$ 100"),
)

#: De onde a foto viria, do mais barato para o mais caro.
CATALOGO = "catalogo"        # zero foto: o ML dá a dele
LOJA = "loja"                # foto própria da Águia
PRECISA_FOTO = "precisa_foto"
ERRO = "erro"


def _faixa_de(preco: float) -> str:
    for chave, minimo, maximo, _ in FAIXAS:
        if minimo <= preco < maximo:
            return chave
    return "baixo"


def sortear(itens: list, amostra: int) -> list:
    """Amostra estratificada por faixa de valor.

    Sorteio simples pegaria quase só peça barata — 914 das 1.687 estão abaixo
    de R$ 100 —, e aí a medição responderia bem a pergunta que menos importa.
    """
    if len(itens) <= amostra:
        return list(itens)

    grupos: dict[str, list] = {c: [] for c, _, _, _ in FAIXAS}
    for item in itens:
        grupos[_faixa_de(float(item["preco"] or 0))].append(item)

    sorteados: list = []
    aleatorio = random.Random(42)          # reprodutível: mesma fila, mesma amostra
    for chave, grupo in grupos.items():
        if not grupo:
            continue
        # proporcional ao tamanho do grupo, com um piso para a faixa não sumir
        quantos = max(10, round(amostra * len(grupo) / len(itens)))
        sorteados.extend(aleatorio.sample(grupo, min(quantos, len(grupo))))

    return sorteados[:amostra + 30]


async def _tem_catalogo(client: MLClient, item) -> tuple[str, str]:
    """A peça tem produto de catálogo utilizável? Devolve (situação, motivo)."""
    try:
        candidatos = await buscar_no_catalogo(
            client, item["sku"] or "", item["descricao_erp"] or "")
    except MLApiError as exc:
        return ERRO, exc.mensagem_amigavel()[:150]

    if not candidatos:
        return PRECISA_FOTO, "sem correspondência no catálogo"

    melhor, recusas = await escolher_publicavel(client, candidatos)
    if melhor is None:
        return PRECISA_FOTO, "; ".join(recusas)[:150]
    return CATALOGO, f"{melhor.nome[:60]} ({melhor.confianca})"


async def medir(lote_id: int, amostra: int = AMOSTRA_PADRAO) -> dict:
    """Mede a origem possível da foto numa amostra da fila. Não escreve nada."""
    fila = storage.itens_por_status(lote_id, "na_fila")
    if not fila:
        return {"erro": "Não há peças na fila deste lote para medir."}

    escolhidos = sortear(fila, amostra)

    # A loja é medida em memória, com UM download do catálogo inteiro — o
    # mesmo caminho que o cruzamento usa, mas sem gravar nada.
    indice, erro_loja, produtos_na_loja = {}, "", 0
    try:
        from app import loja
        produtos = await loja.listar_catalogo()
        produtos_na_loja = len(produtos)
        indice = loja.indexar(produtos)
    except Exception as exc:                              # noqa: BLE001
        erro_loja = str(exc)[:200]
        log.warning("cobertura: loja indisponível: %s", exc)

    client = MLClient()
    semaforo = asyncio.Semaphore(CONCORRENCIA)
    linhas: list[dict] = []

    async def olhar(item) -> None:
        from app import loja

        async with semaforo:
            na_loja = None
            if indice:
                try:
                    na_loja = loja.casar(item["sku"] or "", indice)
                except Exception:                          # noqa: BLE001
                    na_loja = None

            situacao, motivo = await _tem_catalogo(client, item)

            # Ordem de preferência: catálogo (foto zero) > loja (foto nossa).
            if situacao == CATALOGO:
                origem = CATALOGO
            elif na_loja is not None and na_loja.fotos:
                origem = LOJA
            elif situacao == ERRO:
                origem = ERRO
            else:
                origem = PRECISA_FOTO

            linhas.append({
                "sku": item["sku"], "descricao": (item["descricao_erp"] or "")[:60],
                "preco": float(item["preco"] or 0),
                "valor": float(item["valor"] or 0),
                "faixa": _faixa_de(float(item["preco"] or 0)),
                "origem": origem, "motivo": motivo,
                "na_loja": bool(na_loja and na_loja.fotos),
            })

    await asyncio.gather(*(olhar(i) for i in escolhidos))

    return _resumir(linhas, fila, produtos_na_loja, erro_loja)


def _resumir(linhas: list[dict], fila: list, produtos_na_loja: int,
             erro_loja: str) -> dict:
    """Transforma a amostra em número que dá para decidir com ele."""
    total_fila = len(fila)
    medidas = len(linhas)

    por_origem: dict[str, int] = {}
    for linha in linhas:
        por_origem[linha["origem"]] = por_origem.get(linha["origem"], 0) + 1

    # Projeção para a fila inteira, faixa por faixa: a taxa da peça cara não
    # vale para a peça barata, então projetar pela média geral mentiria.
    por_faixa: dict[str, dict] = {}
    for chave, minimo, maximo, rotulo in FAIXAS:
        da_faixa = [l for l in linhas if l["faixa"] == chave]
        na_fila = [i for i in fila
                   if _faixa_de(float(i["preco"] or 0)) == chave]
        if not na_fila:
            continue
        contagem: dict[str, int] = {}
        for linha in da_faixa:
            contagem[linha["origem"]] = contagem.get(linha["origem"], 0) + 1

        por_faixa[chave] = {
            "rotulo": rotulo,
            "na_fila": len(na_fila),
            "medidas": len(da_faixa),
            "valor_parado": sum(float(i["valor"] or 0) for i in na_fila),
            "contagem": contagem,
            "projecao": {
                origem: round(len(na_fila) * n / len(da_faixa))
                for origem, n in contagem.items()} if da_faixa else {},
        }

    projecao_total: dict[str, int] = {}
    for dados in por_faixa.values():
        for origem, n in dados["projecao"].items():
            projecao_total[origem] = projecao_total.get(origem, 0) + n

    return {
        "medido_em": datetime.now(timezone.utc).isoformat(),
        "total_fila": total_fila,
        "medidas": medidas,
        "produtos_na_loja": produtos_na_loja,
        "erro_loja": erro_loja,
        "por_origem": por_origem,
        "por_faixa": por_faixa,
        "projecao_total": projecao_total,
        "valor_total_fila": sum(float(i["valor"] or 0) for i in fila),
        "exemplos_catalogo": [l for l in linhas if l["origem"] == CATALOGO][:8],
        "exemplos_precisa_foto": sorted(
            [l for l in linhas if l["origem"] == PRECISA_FOTO],
            key=lambda l: -l["valor"])[:8],
    }


_EM_ANDAMENTO: set = set()


def iniciar_em_segundo_plano(lote_id: int, amostra: int = AMOSTRA_PADRAO) -> int:
    """Solta a medição num task de fundo e devolve o id para a tela acompanhar.

    São ~150 consultas ao Mercado Livre: passa de qualquer timeout de
    requisição HTTP, então não dá para medir dentro do clique.
    """
    medicao_id = storage.criar_medicao(lote_id, amostra)

    async def _rodar() -> None:
        try:
            resultado = await medir(lote_id, amostra)
            storage.gravar_medicao(medicao_id, "terminada", resultado)
        except Exception as exc:                          # noqa: BLE001
            log.exception("medição de cobertura do lote %s morreu", lote_id)
            storage.gravar_medicao(medicao_id, "erro", {"erro": str(exc)[:400]})

    task = asyncio.create_task(_rodar())
    _EM_ANDAMENTO.add(task)
    task.add_done_callback(_EM_ANDAMENTO.discard)
    return medicao_id
