"""Esteira: processa a fila inteira sozinha, sem alguém clicando peça por peça.

Por que existe: até a v0.24 cada peça exigia a mesma sequência manual —
procurar na internet, abrir o candidato, conferir a ficha, aproveitar, tirar
foto. Isso é razoável para dez peças e impossível para 1.687. O pedido do
João foi direto: *"a questão maior é pegar a lista e realmente automatizar de
forma que fique mais fácil gerar os anúncios a partir da lista da planilha"*.

O que a esteira faz, peça por peça, sem ninguém olhando:

  1. cruza o lote inteiro com a loja da Águia — foto própria, sem questão de
     direito autoral, e é de longe a fonte mais barata;
  2. procura o código nos sites cadastrados (``app/busca.py``);
  3. lê a ficha dos candidatos mais promissores (``app/extrator.py``);
  4. confere se o **código bate de verdade** (``publisher._codigo_confere``);
  5. aproveita só o que a licença permite:

     - domínio autorizado em ``/fontes`` -> ficha + fotos + descrição, e a
       peça fica **pronta para publicar**;
     - qualquer outro domínio -> só **fato técnico** (aplicação, marca), que
       não tem dono, e a peça segue na fila esperando foto.

O QUE ELA NÃO FAZ, DE PROPÓSITO
-------------------------------

**Não publica nada.** Termina em "pronta para publicar"; quem manda para o
Mercado Livre continua sendo a pessoa, na tela de prontas. Publicação
automática em 1.687 peças transformaria um erro de casamento num estrago
irreparável na conta real da Águia Parts.

**Não adivinha peça.** Só aproveita quando o código do ERP aparece na ficha
do site. Nome parecido não vale — foi exatamente assim que um corpo
distribuidor virou um livro na v0.4.0, e a lição foi que informação de risco
não substitui trava.

**Não usa foto de quem não autorizou.** A lista de ``/fontes`` continua sendo
a única porta, e a esteira não a contorna: ela chama as mesmas funções que a
tela usa.

O QUE ELA DEIXA PRONTO PARA A PESSOA
------------------------------------

Para cada peça que a esteira não resolveu sozinha, ela **guarda os candidatos
que encontrou**. O operador abre a lista e clica direto na ficha — não
precisa procurar de novo. É a diferença entre "1.687 peças para pesquisar" e
"120 peças para fotografar, com o link já do lado".
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from app import busca, extrator, storage
from app.abreviacoes import expandir

log = logging.getLogger("aguiahub.esteira")

#: Quantas peças em paralelo. Baixo de propósito: cada peça bate em vários
#: sites, e a Águia não ganha nada sendo bloqueada por excesso de requisição.
CONCORRENCIA = 3

#: Quantas fichas ler por peça. Cada uma é uma requisição a mais; três cobre
#: o caso normal (o mesmo produto em dois ou três sites) sem virar rajada.
MAX_FICHAS = 3

#: Teto de tempo por peça. Um site pendurado não pode parar a esteira inteira.
SEGUNDOS_POR_PECA = 90

#: Resultados possíveis de uma peça, do melhor para o pior.
PRONTA = "pronta"            # ficha + fotos de fonte autorizada
ENRIQUECIDA = "enriquecida"  # fato técnico aproveitado, ainda falta foto
CANDIDATOS = "candidatos"    # achou páginas, mas o código não bateu
NADA = "nada"                # nenhum site conhecido tem essa peça
FALHOU = "falhou"


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _uma_peca(item_id: int, tentar_outros: bool = False) -> str:
    """Processa uma peça e devolve o rótulo do que aconteceu.

    Toda a decisão de aproveitamento é delegada ao ``publisher`` — a esteira
    não tem regra própria de direito autoral nem de casamento de peça. Se um
    dia a regra mudar, muda num lugar só.

    ``tentar_outros`` liga a segunda tentativa quando o código não achou nada:
    os códigos escritos no campo de aplicação e, por último, o nome da peça.
    Desligado por padrão porque multiplica consultas pagas de busca.
    """
    from app.publisher import (_codigo_confere, aplicar_dados_da_pesquisa,
                               usar_ficha_da_pesquisa)

    linha = storage.item(item_id)
    if linha is None:
        return FALHOU

    codigo = (linha["sku"] or "").strip()
    contexto = expandir(linha["descricao_erp"] or "")

    achados = await busca.buscar(codigo, contexto)

    if not achados.candidatos and tentar_outros:
        # Segunda tentativa, só para quem voltou de mãos vazias. A ordem é por
        # precisão: os códigos escritos no campo de aplicação são referência
        # cruzada (equivalente, substituto, remanufaturado) e casam exato; o
        # nome é o último recurso, porque traz muito resultado e muito lixo.
        #
        # Fica atrás de uma opção na tela porque cada tentativa aqui é uma
        # consulta paga na Brave, multiplicada por 1.687 peças.
        for outro in busca.codigos_no_texto(linha["aplicacao"] or "", codigo):
            achados = await busca.buscar(outro, contexto)
            if achados.candidatos:
                break
        else:
            nome = expandir(linha["descricao_erp"] or "")
            if nome:
                achados = await busca.buscar(codigo, contexto, termo=nome)

    if not achados.candidatos:
        storage.marcar_esteira(item_id, NADA,
                               "; ".join(achados.avisos)[:300], [])
        return NADA

    # Quem tem o código no endereço ou no texto do link vem primeiro; se
    # nenhum tiver, ainda vale ler a melhor página — o código costuma estar
    # dentro da ficha mesmo quando não aparece no link.
    com_codigo = [c for c in achados.candidatos if c.confere_codigo]
    alvos = (com_codigo or achados.candidatos)[:MAX_FICHAS]

    guardar = [{"url": c.url, "titulo": c.titulo[:150], "dominio": c.dominio,
                "confere": c.confere_codigo} for c in achados.candidatos[:8]]

    for candidato in alvos:
        try:
            dados = await extrator.extrair(candidato.url)
        except extrator.ExtratorError as exc:
            log.info("esteira item %s: %s", item_id, exc)
            continue
        if not dados.util or not _codigo_confere(codigo, dados):
            continue

        if dados.fotos:
            # Domínio autorizado: ficha, fotos e descrição entram, e a peça
            # sai daqui pronta para publicar.
            usada = usar_ficha_da_pesquisa(item_id, dados)
            if usada.get("ok"):
                storage.marcar_esteira(
                    item_id, PRONTA,
                    f"{len(dados.fotos[:10])} foto(s) e ficha de {dados.dominio}",
                    guardar)
                return PRONTA

        # Sem direito sobre a imagem: aproveita o fato técnico, que é da peça,
        # e a foto fica para a câmera do estoque.
        aplicado = aplicar_dados_da_pesquisa(item_id, dados)
        campos = ", ".join(aplicado.get("campos") or {}) or "nada novo"
        storage.marcar_esteira(
            item_id, ENRIQUECIDA,
            f"{campos} — de {dados.dominio} (sem direito sobre a foto)",
            guardar)
        return ENRIQUECIDA

    storage.marcar_esteira(
        item_id, CANDIDATOS,
        f"{len(guardar)} página(s) encontrada(s), mas o código {codigo} não "
        "apareceu na ficha de nenhuma", guardar)
    return CANDIDATOS


async def rodar(lote_id: int, limite: int = 0, reprocessar: bool = False,
                execucao_id: int | None = None,
                tentar_outros: bool = False) -> dict:
    """Roda a esteira no lote inteiro. Feita para rodar em segundo plano.

    ``limite`` processa só as N peças de maior valor parado — serve para uma
    primeira rodada curta, antes de soltar nas 1.687.

    ``tentar_outros`` repassa a segunda tentativa (ver ``_uma_peca``) para
    cada peça que voltar vazia da busca pelo código.
    """
    contagem = {PRONTA: 0, ENRIQUECIDA: 0, CANDIDATOS: 0, NADA: 0, FALHOU: 0}

    # Passo 0: a loja da Águia. É a fonte de foto mais barata e a única sem
    # nenhuma questão de licença — vale sempre tentar antes de sair pela
    # internet. Uma falha aqui (site fora do ar) não pode parar o resto.
    da_loja = 0
    try:
        from app.publisher import cruzar_lote_com_loja
        resumo = await cruzar_lote_com_loja(lote_id)
        da_loja = int(resumo.get("com_foto") or 0)
    except Exception as exc:                              # noqa: BLE001
        log.warning("esteira: cruzamento com a loja falhou: %s", exc)

    pendentes = storage.itens_para_esteira(lote_id, limite, reprocessar)
    total = len(pendentes)

    if execucao_id is None:
        execucao_id = storage.criar_execucao(lote_id, total)
    storage.atualizar_execucao(execucao_id, total=total, da_loja=da_loja)

    semaforo = asyncio.Semaphore(CONCORRENCIA)
    processados = 0
    trava = asyncio.Lock()

    async def tratar(item_id: int) -> None:
        nonlocal processados
        async with semaforo:
            if storage.execucao_parando(execucao_id):
                return
            try:
                rotulo = await asyncio.wait_for(
                    _uma_peca(item_id, tentar_outros),
                    timeout=SEGUNDOS_POR_PECA)
            except asyncio.TimeoutError:
                storage.marcar_esteira(item_id, FALHOU,
                                       "os sites demoraram demais", [])
                rotulo = FALHOU
            except Exception as exc:                      # noqa: BLE001
                log.warning("esteira item %s falhou: %s", item_id, exc)
                storage.marcar_esteira(item_id, FALHOU, str(exc)[:300], [])
                rotulo = FALHOU

            async with trava:
                processados += 1
                contagem[rotulo] = contagem.get(rotulo, 0) + 1
                storage.atualizar_execucao(
                    execucao_id, processados=processados,
                    prontas=contagem[PRONTA], enriquecidas=contagem[ENRIQUECIDA],
                    com_candidatos=contagem[CANDIDATOS],
                    sem_resultado=contagem[NADA], falhas=contagem[FALHOU])

    await asyncio.gather(*(tratar(r["id"]) for r in pendentes))

    parada = storage.execucao_parando(execucao_id)
    storage.atualizar_execucao(
        execucao_id,
        status="parada" if parada else "terminada",
        terminado_em=_agora(),
        mensagem=("Parada a pedido." if parada else
                  f"{contagem[PRONTA]} peça(s) prontas para publicar, "
                  f"{contagem[ENRIQUECIDA]} com dados novos esperando foto."))

    return {"execucao_id": execucao_id, "total": total, "da_loja": da_loja,
            **contagem}


#: Referência forte para os tasks em andamento. Sem isto o coletor de lixo do
#: Python pode recolher o task no meio do caminho — ``create_task`` só guarda
#: referência fraca, e a esteira morreria silenciosamente depois de algumas
#: peças, deixando a tela mostrando "rodando" para sempre.
_EM_ANDAMENTO: set = set()


def iniciar_em_segundo_plano(lote_id: int, limite: int = 0,
                             reprocessar: bool = False,
                             tentar_outros: bool = False) -> int:
    """Cria a execução e solta a esteira num task de fundo.

    Devolve o id da execução na hora, para a tela já poder acompanhar. O
    processo é longo (1.687 peças levam horas) e nenhuma requisição HTTP pode
    ficar esperando isso.
    """
    total = len(storage.itens_para_esteira(lote_id, limite, reprocessar))
    execucao_id = storage.criar_execucao(lote_id, total)

    async def _rodar() -> None:
        try:
            await rodar(lote_id, limite, reprocessar, execucao_id,
                        tentar_outros)
        except Exception as exc:                          # noqa: BLE001
            log.exception("esteira do lote %s morreu", lote_id)
            storage.atualizar_execucao(
                execucao_id, status="erro", terminado_em=_agora(),
                mensagem=f"A esteira parou com erro: {exc}")

    task = asyncio.create_task(_rodar())
    _EM_ANDAMENTO.add(task)
    task.add_done_callback(_EM_ANDAMENTO.discard)
    return execucao_id


def resumo_para_tela(lote_id: int) -> dict:
    """O que a tela da esteira mostra: execução atual (ou última) e a fila."""
    execucao = storage.ultima_execucao(lote_id)
    return {
        "execucao": execucao,
        "rodando": bool(execucao and execucao["status"] in ("rodando", "parando")),
        "progresso": storage.progresso_da_fila(lote_id),
        "sem_foto": storage.itens_da_esteira(lote_id, limite=200),
        "fontes": sorted(extrator.fontes_permitidas()),
        "sites": busca.sites_para_consultar(),
    }
