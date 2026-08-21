"""Águiahub — publicação de anúncios no Mercado Livre a partir da planilha do ERP."""
from __future__ import annotations

import csv
import io
import json
import logging
import secrets
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app import storage
from app.config import get_settings
from app.version import VERSAO, LANCADA_EM
from app.ml import oauth
from app.ml.client import MLClient, MLApiError
from app.ml.catalog import (CaminhoDoMLFechado, buscar_no_catalogo,
                            escolher_publicavel)
from app import loja
from app.extrator import fontes_permitidas
from app import fotos as mod_fotos
from app.publisher import (analisar_lote, cruzar_lote_com_loja,
                           publicar_aprovados, publicar_com_fotos_proprias,
                           corrigir_fotos, publicar_da_loja,
                           publicar_selecionados,
                           aplicar_dados_da_pesquisa, pesquisar_em_site,
                           usar_ficha_da_pesquisa,
                           resumir, testar_item, vincular_por_link,
                           vincular_qualquer,
                           vincular_produto_da_loja)
from app.sheets import (FORMATOS_ACEITOS, FormatoNaoSuportado, LinhaProduto,
                        calcular_preco, ler_planilha, montar_descricao,
                        montar_titulo, resumo_da_selecao)
from app.triagem import triar_planilha, resumo as resumo_triagem

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s — %(message)s")
log = logging.getLogger("aguiahub")

s = get_settings()
app = FastAPI(title="Águiahub", version=VERSAO, docs_url="/api/docs")

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
templates.env.globals["VERSAO"] = VERSAO
# usado pelo cabeçalho para avisar, em QUALQUER tela, que falta conectar
templates.env.globals["conta_conectada"] = lambda: bool(storage.carregar_token())
# avisa em qualquer tela se o banco NÃO está num volume — sem isso o deploy
# apaga silenciosamente as decisões já tomadas na fila
templates.env.globals["persistencia"] = storage.diagnostico_persistencia
templates.env.filters["fromjson"] = lambda t: __import__("json").loads(t or "[]")


@app.exception_handler(HTTPException)
async def erro_amigavel(request: Request, exc: HTTPException):
    """Erro vira página com saída, não JSON cru.

    Quem opera é a pessoa do estoque: uma tela de JSON sem botão de voltar é um
    beco sem saída. Se o problema for falta de conexão com o ML, a própria
    página oferece o botão para conectar.
    """
    if request.url.path.startswith("/api") or exc.status_code == 404 and \
            request.headers.get("accept", "").startswith("application/json"):
        return await http_exception_handler(request, exc)

    mensagem = exc.detail if isinstance(exc.detail, str) else "Erro inesperado."
    precisa_conectar = "Mercado Livre primeiro" in mensagem or not storage.carregar_token()
    return templates.TemplateResponse(
        request, "erro.html",
        {"request": request, "mensagem": mensagem,
         "precisa_conectar": precisa_conectar},
        status_code=exc.status_code,
    )


ABERTAS = ("/health", "/entrar", "/static")


@app.middleware("http")
async def exigir_senha(request: Request, call_next):
    """Protege a aplicação com uma senha única, se APP_PASSWORD estiver definida.

    Desligado por padrão (variável vazia), conforme escolha do cliente. Basta
    definir APP_PASSWORD no EasyPanel para ativar — a pessoa digita uma vez por
    navegador e a sessão dura.

    Importa porque a URL é pública na internet e a aplicação publica anúncios
    reais: sem senha, quem tiver o endereço pode anunciar na conta da Águia.
    """
    if not s.app_password or request.url.path.startswith(ABERTAS):
        return await call_next(request)
    if request.session.get("autenticado"):
        return await call_next(request)
    return RedirectResponse("/entrar", status_code=303)


# A sessão é registrada depois da checagem acima de propósito: no Starlette o
# middleware registrado por último é o mais externo, então assim a sessão já
# está disponível quando `exigir_senha` roda.
app.add_middleware(SessionMiddleware, secret_key=s.secret_key, https_only=True)


@app.get("/entrar", response_class=HTMLResponse)
async def form_entrar(request: Request, erro: str = ""):
    return templates.TemplateResponse(request, "entrar.html",
                                      {"request": request, "erro": erro})


@app.post("/entrar")
async def entrar(request: Request, senha: str = Form(...)):
    if s.app_password and secrets.compare_digest(senha, s.app_password):
        request.session["autenticado"] = True
        return RedirectResponse("/", status_code=303)
    return RedirectResponse("/entrar?erro=1", status_code=303)


@app.on_event("startup")
def _startup() -> None:
    storage.init_db()
    log.info("Águiahub v%s (%s) iniciando", VERSAO, LANCADA_EM)
    for problema in s.validate():
        log.warning("Configuração: %s", problema)


# ---------------------------------------------------------------------------
# Saúde e conexão
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "versao": VERSAO, "lancada_em": LANCADA_EM}


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    conta = storage.carregar_token()
    info = None
    diagnostico = None
    if conta:
        bundle, nickname = conta
        info = {"user_id": bundle.user_id, "nickname": nickname,
                "expira_em": bundle.expires_at.isoformat()}
        # Mostra impedimentos da conta (endereço faltando, restrições) aqui,
        # em vez de deixar o operador descobrir só na hora de publicar.
        try:
            diagnostico = await MLClient().diagnostico_conta()
        except Exception as exc:                      # noqa: BLE001
            log.warning("Não foi possível diagnosticar a conta: %s", exc)

    return templates.TemplateResponse(request, "home.html", {
        "request": request,
        "conta": info,
        "diagnostico": diagnostico,
        "lotes": storage.listar_lotes(),
        "problemas_config": s.validate(),
        # domínios autorizados a ceder imagem: os do banco e os do ambiente.
        # Fica visível para dar para confirmar sem precisar testar um link.
        "fontes_imagem": sorted(fontes_permitidas()),
    })


@app.get("/oauth/mercadolivre/login")
async def login(request: Request):
    state = secrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    verifier = challenge = None
    if s.ml_use_pkce:
        verifier, challenge = oauth.gerar_pkce()
        request.session["pkce_verifier"] = verifier
    return RedirectResponse(oauth.montar_url_autorizacao(state, challenge))


@app.get("/oauth/mercadolivre/callback", response_class=HTMLResponse)
async def callback(request: Request, code: str | None = None,
                   state: str | None = None, error: str | None = None):
    if error:
        raise HTTPException(400, f"Mercado Livre recusou a autorização: {error}")
    if not code:
        raise HTTPException(400, "Callback sem o parâmetro 'code'.")

    esperado = request.session.pop("oauth_state", None)
    if not esperado or not secrets.compare_digest(esperado, state or ""):
        # protege contra CSRF no fluxo de autorização
        raise HTTPException(400, "State inválido — refaça a conexão.")

    verifier = request.session.pop("pkce_verifier", None)
    bundle = await oauth.trocar_code_por_token(code, verifier)

    storage.salvar_token(bundle)
    try:
        eu = await MLClient().eu()
        storage.salvar_token(bundle, eu.get("nickname"))
    except MLApiError as exc:
        log.warning("Token salvo, mas /users/me falhou: %s", exc)

    return RedirectResponse("/", status_code=303)


@app.post("/oauth/mercadolivre/desconectar")
async def desconectar():
    storage.desconectar()
    return RedirectResponse("/", status_code=303)


# ---------------------------------------------------------------------------
# Planilha
# ---------------------------------------------------------------------------

@app.post("/planilha/previa", response_class=HTMLResponse)
async def previa(request: Request, arquivo: UploadFile = File(...),
                 regra_preco: str = Form("preco_publico"),
                 percentual: float = Form(0.0)):
    if not arquivo.filename.lower().endswith(FORMATOS_ACEITOS):
        raise HTTPException(
            400,
            f"'{arquivo.filename}' não é uma planilha reconhecida. "
            f"Formatos aceitos: {', '.join(FORMATOS_ACEITOS)}. "
            "Google Sheets, LibreOffice e WPS exportam para .xlsx ou .csv."
        )

    destino = Path(s.data_dir) / "uploads"
    destino.mkdir(parents=True, exist_ok=True)
    # evita path traversal via nome de arquivo malicioso ("../../etc/x")
    caminho = destino / Path(arquivo.filename).name
    caminho.write_bytes(await arquivo.read())

    try:
        itens = ler_planilha(caminho)
    except FormatoNaoSuportado as exc:
        raise HTTPException(400, str(exc))
    linhas = [{
        "aba": i.aba,
        "linha": i.linha,
        "codigo": i.codigo,
        "descricao": i.descricao,
        "titulo": montar_titulo(i),
        "quantidade": i.quantidade,
        "preco": calcular_preco(i, regra_preco, percentual),
        "problemas": i.problemas,
    } for i in itens]

    abas: dict[str, int] = {}
    for i in itens:
        abas[i.aba] = abas.get(i.aba, 0) + 1

    return templates.TemplateResponse(request, "previa.html", {
        "request": request,
        "arquivo": arquivo.filename,
        "linhas": linhas[:500],
        "total": len(linhas),
        "abas": abas,
        "com_problema": sum(1 for i in itens if i.problemas),
        "regra_preco": regra_preco,
        "percentual": percentual,
        "selecao": resumo_da_selecao(itens),
    })


@app.post("/planilha/analisar")
async def analisar(request: Request, arquivo_salvo: str = Form(...),
                   regra_preco: str = Form("preco_publico"),
                   percentual: float = Form(0.0),
                   abas: str = Form(""),
                   limite: int = Form(0)):
    """Consulta o catálogo do ML e monta a conferência. NÃO publica nada."""
    if not storage.carregar_token():
        raise HTTPException(400, "Conecte a conta do Mercado Livre primeiro.")

    caminho = Path(s.data_dir) / "uploads" / Path(arquivo_salvo).name
    if not caminho.exists():
        raise HTTPException(404, "Planilha não encontrada — envie novamente.")

    filtro = [a.strip() for a in abas.split("|") if a.strip()] or None
    try:
        itens = ler_planilha(caminho, filtro)
    except FormatoNaoSuportado as exc:
        raise HTTPException(400, str(exc))
    if limite > 0:
        itens = itens[:limite]

    lote_id = storage.criar_lote(arquivo_salvo, len(itens), True,
                                 {"regra_preco": regra_preco, "percentual": percentual})
    await analisar_lote(itens, regra_preco=regra_preco, percentual=percentual,
                        lote_id=lote_id)
    storage.atualizar_status_lote(lote_id, "aguardando_aprovacao")

    return RedirectResponse(f"/lotes/{lote_id}/conferencia", status_code=303)


@app.get("/lotes/{lote_id}/conferencia", response_class=HTMLResponse)
async def conferencia(request: Request, lote_id: int):
    lote = storage.lote(lote_id)
    if not lote:
        raise HTTPException(404, "Lote não encontrado.")

    itens = storage.itens_do_lote(lote_id)
    resumo: dict[str, int] = {}
    for i in itens:
        resumo[i["status"]] = resumo.get(i["status"], 0) + 1

    return templates.TemplateResponse(request, "conferencia.html", {
        "request": request,
        "lote": lote,
        "aguardando": [i for i in itens if i["status"] == "aguardando_aprovacao"],
        "outros": [i for i in itens if i["status"] != "aguardando_aprovacao"],
        "resumo": resumo,
    })


# ---------------------------------------------------------------------------
# Triagem e fila de trabalho
# ---------------------------------------------------------------------------

@app.post("/planilha/triagem")
async def triagem(request: Request, arquivo_salvo: str = Form(...),
                  regra_preco: str = Form("preco_publico"),
                  percentual: float = Form(0.0),
                  abas: str = Form("")):
    """Classifica a planilha inteira SEM consultar o Mercado Livre.

    Consultar o ML para 1.687 itens custaria milhares de requisições só para
    descobrir que a maioria nem tem código utilizável. A consulta acontece
    depois, um item por vez, quando o operador chega nele.
    """
    caminho = Path(s.data_dir) / "uploads" / Path(arquivo_salvo).name
    if not caminho.exists():
        raise HTTPException(404, "Planilha não encontrada — envie novamente.")

    filtro = [a.strip() for a in abas.split("|") if a.strip()] or None
    try:
        itens = ler_planilha(caminho, filtro)
    except FormatoNaoSuportado as exc:
        raise HTTPException(400, str(exc))

    triados = triar_planilha(itens)
    lote_id = storage.criar_lote(arquivo_salvo, len(triados), True,
                                 {"regra_preco": regra_preco,
                                  "percentual": percentual, "tipo": "fila"})

    # Se a planilha trouxer coluna de seleção ("Anunciar", "Publicar"...), só
    # as linhas marcadas entram na fila. Quem conhece o estoque é o João — o
    # sistema não sabe que uma peça cara é encalhe insalvável.
    selecao = resumo_da_selecao(itens)

    for t in triados:
        if not t.pronto:
            status_inicial = "sem_dado"
        elif selecao["tem_coluna"] and not t.item.selecionada:
            status_inicial = "nao_selecionado"
        else:
            status_inicial = "na_fila"

        storage.registrar_item(
            lote_id, t.item.linha, t.item.codigo, montar_titulo(t.item),
            payload={},
            status=status_inicial,
            erro="; ".join(t.impedimentos) or None,
            descricao_erp=t.item.descricao,
            marca=t.item.marca,
            aplicacao=t.item.aplicacao,
            quantidade=t.item.quantidade,
            preco=calcular_preco(t.item, regra_preco, percentual),
            valor=t.valor,
            qualidade_codigo=t.qualidade,
        )

    storage.atualizar_status_lote(lote_id, "fila")
    return RedirectResponse(f"/fila/{lote_id}", status_code=303)


@app.get("/fila/{lote_id}", response_class=HTMLResponse)
async def fila(request: Request, lote_id: int):
    """Uma peça por tela, sempre a de maior valor ainda pendente."""
    lote = storage.lote(lote_id)
    if not lote:
        raise HTTPException(404, "Lote não encontrado.")

    item = storage.proximo_da_fila(lote_id)
    sugestao = None
    da_loja: list = []

    # A loja da Águia vem primeiro: se a peça está lá, temos foto própria e o
    # anúncio sai sem depender do catálogo do Mercado Livre.
    if item is not None:
        try:
            da_loja = await loja.buscar_por_codigo(item["sku"] or "")
        except Exception as exc:                       # noqa: BLE001
            log.warning("Busca na loja falhou para %s: %s", item["sku"], exc)

    if item is not None and not da_loja and storage.carregar_token():
        # Consulta o catálogo só agora, para ESTE item. Se falhar, a tela
        # continua útil: a pessoa procura no ML pelo botão e cola o link.
        try:
            candidatos = await buscar_no_catalogo(
                MLClient(), item["sku"] or "", item["descricao_erp"] or "")
            if candidatos:
                melhor, _ = await escolher_publicavel(MLClient(), candidatos)
                sugestao = melhor
        except Exception as exc:                       # noqa: BLE001
            log.warning("Busca do item %s falhou: %s", item["id"], exc)

    return templates.TemplateResponse(request, "fila.html", {
        "request": request,
        "lote": lote,
        "item": item,
        "sugestao": sugestao,
        "da_loja": da_loja,
        "fotos_proprias": mod_fotos.listar(item["id"]) if item else [],
        "progresso": storage.progresso_da_fila(lote_id),
    })


@app.get("/lotes/{lote_id}/prontas", response_class=HTMLResponse)
async def prontas(request: Request, lote_id: int):
    """Lista as peças que já têm foto e dados — e permite publicar em lote."""
    if not storage.lote(lote_id):
        raise HTTPException(404, "Lote não encontrado.")
    itens = storage.itens_prontos_para_publicar(lote_id)
    return templates.TemplateResponse(request, "prontas.html", {
        "request": request, "lote_id": lote_id, "itens": itens,
        "valor_total": sum(float(i["valor"] or 0) for i in itens),
        "resultado": None,
    })


@app.get("/fontes", response_class=HTMLResponse)
async def fontes(request: Request, erro: str = ""):
    """Gestão das fontes de imagem autorizadas, sem passar pelo EasyPanel."""
    return templates.TemplateResponse(request, "fontes.html", {
        "request": request,
        "fontes": storage.listar_fontes(),
        "do_ambiente": [d.strip() for d in
                        (s.fontes_imagem_autorizadas or "").split(",") if d.strip()],
        "erro": erro,
    })


@app.post("/fontes")
async def autorizar_fonte(dominio: str = Form(...), observacao: str = Form(""),
                          voltar_para: str = Form("")):
    try:
        storage.autorizar_fonte(dominio, observacao)
    except ValueError as exc:
        return RedirectResponse(f"/fontes?erro={quote(str(exc))}", status_code=303)
    return RedirectResponse(voltar_para or "/fontes", status_code=303)


@app.post("/fontes/remover")
async def remover_fonte(dominio: str = Form(...)):
    storage.remover_fonte(dominio)
    return RedirectResponse("/fontes", status_code=303)


@app.get("/lotes/{lote_id}/decididas", response_class=HTMLResponse)
async def decididas(request: Request, lote_id: int, status: str = "",
                    busca: str = ""):
    """Histórico do que já foi decidido, com opção de rever cada peça."""
    if not storage.lote(lote_id):
        raise HTTPException(404, "Lote não encontrado.")

    progresso = storage.progresso_da_fila(lote_id)
    contagem = {k: v["itens"] for k, v in progresso.items()
                if not k.startswith("_") and isinstance(v, dict)}

    return templates.TemplateResponse(request, "decididas.html", {
        "request": request, "lote_id": lote_id,
        "itens": storage.itens_decididos(lote_id, status, busca),
        "fora": storage.itens_fora_da_fila(lote_id),
        "filtro": status, "busca": busca, "contagem": contagem,
    })


@app.post("/itens/{item_id}/reabrir")
async def reabrir(item_id: int, lote_id: int = Form(...),
                  volta_para: str = Form("fila")):
    """Devolve a peça para a fila — para rever uma decisão ou trazer de volta
    uma que tinha ficado fora da seleção da planilha."""
    if not storage.reabrir_item(item_id):
        raise HTTPException(400, (
            "Esta peça já foi publicada no Mercado Livre. Desfazer aqui não "
            "apagaria o anúncio de lá — para tirar do ar, faça isso pelo "
            "próprio Mercado Livre."))
    destino = (f"/lotes/{lote_id}/decididas" if volta_para == "decididas"
               else f"/fila/{lote_id}")
    return RedirectResponse(destino, status_code=303)


@app.post("/itens/{item_id}/corrigir-fotos")
async def rota_corrigir_fotos(item_id: int, lote_id: int = Form(...)):
    """Coloca as fotos num anúncio já publicado que ficou sem imagem."""
    if not storage.carregar_token():
        raise HTTPException(400, "Conecte a conta do Mercado Livre primeiro.")
    resultado = await corrigir_fotos(item_id)
    if not resultado["ok"]:
        raise HTTPException(400, resultado["mensagem"])
    return RedirectResponse(f"/lotes/{lote_id}/decididas?status=publicado",
                            status_code=303)


@app.post("/itens/{item_id}/pesquisar", response_class=HTMLResponse)
async def pesquisar(request: Request, item_id: int, url: str = Form(...),
                    lote_id: int = Form(...)):
    """Lê a ficha da peça num site qualquer e mostra para conferir."""
    resultado = await pesquisar_em_site(item_id, url)
    if not resultado["ok"]:
        raise HTTPException(400, resultado["mensagem"])
    return templates.TemplateResponse(request, "pesquisa.html", {
        "request": request, "lote_id": lote_id,
        "item": resultado["item"], "dados": resultado["dados"],
        "confere_codigo": resultado["confere_codigo"],
    })


@app.post("/itens/{item_id}/usar-ficha")
async def usar_ficha(item_id: int, url: str = Form(...),
                     lote_id: int = Form(...)):
    """Deixa a peça pronta com a ficha e as fotos do site pesquisado."""
    resultado = await pesquisar_em_site(item_id, url)
    if not resultado["ok"]:
        raise HTTPException(400, resultado["mensagem"])
    usada = usar_ficha_da_pesquisa(item_id, resultado["dados"])
    if not usada["ok"]:
        raise HTTPException(400, usada["mensagem"])
    # Vai direto para a tela de pré-anúncio: dali dá para revisar o texto
    # trazido do site e trocar a foto antes de publicar de verdade.
    return RedirectResponse(f"/itens/{item_id}/preparar?lote_id={lote_id}",
                            status_code=303)


@app.post("/itens/{item_id}/aplicar-pesquisa")
async def aplicar_pesquisa(item_id: int, url: str = Form(...),
                           lote_id: int = Form(...)):
    """Guarda na peça os fatos técnicos confirmados pela pessoa.

    Antes (até v0.22.0) isto voltava para `/fila/{lote_id}`, que mostra a
    peça de MAIOR valor pendente — nem sempre a mesma que acabou de ser
    pesquisada. O João reportou o efeito: "não está levando para o processo
    de criar o anúncio". Agora vai direto para a tela de pré-anúncio desta
    peça, onde dá para ver o que foi aproveitado e seguir com foto própria.
    """
    resultado = await pesquisar_em_site(item_id, url)
    if not resultado["ok"]:
        raise HTTPException(400, resultado["mensagem"])
    aplicar_dados_da_pesquisa(item_id, resultado["dados"])
    return RedirectResponse(f"/itens/{item_id}/preparar?lote_id={lote_id}",
                            status_code=303)


@app.get("/itens/{item_id}/preparar", response_class=HTMLResponse)
async def preparar(request: Request, item_id: int, lote_id: int):
    """Tela de pré-anúncio: revisar/editar título e descrição, e gerenciar
    fotos, antes de publicar de verdade no Mercado Livre.

    Nada aqui publica nada — é só o que fica pronto pra mandar quando o
    operador clicar em publicar, na fila ou na lista de prontas.
    """
    linha = storage.item(item_id)
    if linha is None:
        raise HTTPException(404, "Peça não encontrada.")

    p = LinhaProduto(
        aba="", linha=linha["linha"], codigo=linha["sku"] or "",
        descricao=linha["descricao_erp"] or "",
        quantidade=int(linha["quantidade"] or 1),
        marca=linha["marca"] or "",
        aplicacao=linha["aplicacao"] or "",
    )
    titulo_padrao = (linha["loja_nome"] or montar_titulo(p))[:60]
    descricao_padrao = linha["loja_descricao"] or montar_descricao(p)
    fotos_site = json.loads(linha["loja_fotos"] or "[]")
    fotos_proprias = mod_fotos.listar(item_id)

    return templates.TemplateResponse(request, "preparar.html", {
        "request": request, "lote_id": lote_id, "item": linha,
        "titulo_atual": linha["titulo_editado"] or titulo_padrao,
        "descricao_atual": linha["descricao_editada"] or descricao_padrao,
        "fotos_site": fotos_site,
        "fotos_proprias": fotos_proprias,
        "pronto": bool(fotos_site or fotos_proprias),
    })


@app.post("/itens/{item_id}/preparar")
async def salvar_preparo(item_id: int, lote_id: int = Form(...),
                         titulo: str = Form(""), descricao: str = Form("")):
    """Grava o título e a descrição editados pelo operador na tela de pré-anúncio."""
    if storage.item(item_id) is None:
        raise HTTPException(404, "Peça não encontrada.")
    storage.atualizar_dados_catalogo(
        item_id,
        titulo_editado=titulo.strip()[:60] or None,
        descricao_editada=descricao.strip()[:4000] or None,
    )
    return RedirectResponse(f"/itens/{item_id}/preparar?lote_id={lote_id}",
                            status_code=303)


@app.post("/itens/{item_id}/loja-fotos/remover")
async def remover_foto_do_site(item_id: int, lote_id: int = Form(...),
                               url: str = Form(...)):
    """Tira uma foto vinda da loja/site da lista do pré-anúncio."""
    linha = storage.item(item_id)
    if linha is None:
        raise HTTPException(404, "Peça não encontrada.")
    restantes = [f for f in json.loads(linha["loja_fotos"] or "[]") if f != url]
    storage.atualizar_dados_catalogo(
        item_id, loja_fotos=json.dumps(restantes, ensure_ascii=False))
    return RedirectResponse(f"/itens/{item_id}/preparar?lote_id={lote_id}",
                            status_code=303)


@app.get("/itens/{item_id}/testar", response_class=HTMLResponse)
async def testar(request: Request, item_id: int):
    """Pergunta ao Mercado Livre se ele aceitaria o anúncio — sem publicar."""
    linha = storage.item(item_id)
    if linha is None:
        raise HTTPException(404, "Peça não encontrada.")
    if not storage.carregar_token():
        raise HTTPException(400, "Conecte a conta do Mercado Livre primeiro.")

    resultado = await testar_item(item_id)
    return templates.TemplateResponse(request, "teste.html", {
        "request": request, "item": linha, "resultado": resultado,
    })


@app.post("/lotes/{lote_id}/publicar-selecionadas", response_class=HTMLResponse)
async def publicar_selecionadas(request: Request, lote_id: int,
                                ids: list[int] = Form(default=[]),
                                confirmacao: str = Form("")):
    """Publica no Mercado Livre as peças marcadas. Cria anúncios REAIS."""
    if not storage.lote(lote_id):
        raise HTTPException(404, "Lote não encontrado.")
    # A confirmação digitada saiu a pedido do João (v0.17.0): na prática ele
    # digitava "publicar" em toda peça e a fricção só atrasava. O parâmetro
    # continua aceito para não quebrar link antigo, mas não é mais exigido.
    # A proteção que resta é o aviso na tela e o confirm do navegador.
    if not storage.carregar_token():
        raise HTTPException(400, "Conecte a conta do Mercado Livre primeiro.")

    resultado = await publicar_selecionados(ids)

    itens = storage.itens_prontos_para_publicar(lote_id)
    return templates.TemplateResponse(request, "prontas.html", {
        "request": request, "lote_id": lote_id, "itens": itens,
        "valor_total": sum(float(i["valor"] or 0) for i in itens),
        "resultado": resultado,
    })


@app.post("/lotes/{lote_id}/cruzar-loja", response_class=HTMLResponse)
async def cruzar_loja(request: Request, lote_id: int):
    """Cruza o lote inteiro com o catálogo da loja da Águia, de uma vez."""
    if not storage.lote(lote_id):
        raise HTTPException(404, "Lote não encontrado.")
    try:
        resumo = await cruzar_lote_com_loja(lote_id)
    except loja.LojaError as exc:
        raise HTTPException(400, str(exc))

    return templates.TemplateResponse(request, "cruzamento.html", {
        "request": request, "lote_id": lote_id, "resumo": resumo,
    })


@app.get("/diagnostico/loja", response_class=HTMLResponse)
async def diagnostico_loja(request: Request, referencia: str = ""):
    """Mostra exatamente o que a loja respondeu — status, tipo e corpo.

    Quando um vínculo falha no VPS, sem isto só sobra adivinhar. Aqui o erro
    aparece cru: HTTP 403 é firewall, 404 é API desativada, HTML em vez de JSON
    é página de bloqueio.
    """
    contexto = {"request": request, "referencia": referencia,
                "base": s.loja_base_url, "diag": None,
                "produto": None, "mensagem": None}

    if referencia.strip():
        params = ({"slug": loja._slug_da_url(referencia)}
                  if referencia.strip().startswith("http")
                  else {"sku": referencia.strip()})
        try:
            dados, diag = await loja.consultar_bruto(params)
            contexto["diag"] = diag
            if dados:
                contexto["produto"] = loja._montar(dados[0], s.loja_base_url)
            else:
                contexto["mensagem"] = (
                    "A loja respondeu certo, mas não achou nenhum produto com "
                    f"esse {'endereço' if 'slug' in params else 'código'}.")
        except loja.LojaError as exc:
            contexto["mensagem"] = str(exc)

    return templates.TemplateResponse(request, "diag_loja.html", contexto)


@app.post("/itens/{item_id}/vincular-loja")
async def vincular_loja(item_id: int, referencia: str = Form(...),
                        lote_id: int = Form(...)):
    """Puxa nome, descrição e fotos do site da Águia para o item."""
    try:
        resultado = await vincular_produto_da_loja(item_id, referencia)
    except loja.LojaError as exc:
        raise HTTPException(400, str(exc))
    if not resultado["ok"]:
        raise HTTPException(400, resultado["mensagem"])
    return RedirectResponse(f"/fila/{lote_id}", status_code=303)


@app.post("/itens/{item_id}/publicar-loja")
async def publicar_item_da_loja(item_id: int, lote_id: int = Form(...),
                                confirmacao: str = Form("")):
    """Cria o anúncio no ML com as fotos da loja. Cria anúncio REAL."""
    if not storage.carregar_token():
        raise HTTPException(400, "Conecte a conta do Mercado Livre primeiro.")
    # Confirmação digitada removida a pedido do João (v0.17.0).

    resultado = await publicar_da_loja(item_id)
    if not resultado["ok"]:
        raise HTTPException(400, resultado["mensagem"])
    return RedirectResponse(f"/fila/{lote_id}", status_code=303)


def _destino_fotos(item_id: int, lote_id: int, volta_para: str) -> str:
    if volta_para == "preparar":
        return f"/itens/{item_id}/preparar?lote_id={lote_id}"
    return f"/fila/{lote_id}"


@app.post("/itens/{item_id}/fotos")
async def enviar_fotos(item_id: int, lote_id: int = Form(...),
                       arquivos: list[UploadFile] = File(...),
                       volta_para: str = Form("fila")):
    """Recebe as fotos tiradas pelo operador.

    Na tela de pré-anúncio, essas fotos SUBSTITUEM a da loja/site quando
    chega a hora de publicar — não somam.
    """
    enviadas, problemas = 0, []
    for arquivo in arquivos:
        try:
            mod_fotos.salvar(item_id, await arquivo.read(),
                             arquivo.filename or "foto.jpg")
            enviadas += 1
        except mod_fotos.FotoInvalida as exc:
            problemas.append(str(exc))

    if not enviadas and problemas:
        raise HTTPException(400, " ".join(problemas))
    return RedirectResponse(_destino_fotos(item_id, lote_id, volta_para),
                            status_code=303)


@app.post("/itens/{item_id}/fotos/{nome}/apagar")
async def apagar_foto(item_id: int, nome: str, lote_id: int = Form(...),
                      volta_para: str = Form("fila")):
    try:
        mod_fotos.apagar(item_id, nome)
    except mod_fotos.FotoInvalida as exc:
        raise HTTPException(400, str(exc))
    return RedirectResponse(_destino_fotos(item_id, lote_id, volta_para),
                            status_code=303)


@app.get("/fotos/{item_id}/{nome}")
async def ver_foto(item_id: int, nome: str):
    """Serve a foto salva, para a prévia na tela."""
    try:
        caminho = mod_fotos.caminho_da_foto(item_id, nome)
    except mod_fotos.FotoInvalida as exc:
        raise HTTPException(400, str(exc))
    if not caminho.is_file():
        raise HTTPException(404, "Foto não encontrada.")
    tipos = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
             ".webp": "image/webp", ".gif": "image/gif"}
    return Response(caminho.read_bytes(),
                    media_type=tipos.get(caminho.suffix.lower(), "image/jpeg"))


@app.post("/itens/{item_id}/publicar-fotos")
async def publicar_com_foto(item_id: int, lote_id: int = Form(...),
                            confirmacao: str = Form("")):
    if not storage.carregar_token():
        raise HTTPException(400, "Conecte a conta do Mercado Livre primeiro.")
    # Confirmação digitada removida a pedido do João (v0.17.0).
    resultado = await publicar_com_fotos_proprias(item_id)
    if not resultado["ok"]:
        raise HTTPException(400, resultado["mensagem"])
    return RedirectResponse(f"/fila/{lote_id}", status_code=303)


@app.post("/itens/{item_id}/decidir")
async def decidir(item_id: int, decisao: str = Form(...), lote_id: int = Form(...)):
    """Registra a decisão do operador e passa para a próxima peça."""
    mapa = {
        "nao_achei": ("nao_encontrado",
                      "operador procurou no ML e não encontrou a peça"),
        "duvida": ("em_duvida",
                   "operador ficou em dúvida — separado para revisão"),
        "pular": ("na_fila", None),
    }
    if decisao not in mapa:
        raise HTTPException(400, "Decisão inválida.")

    status, nota = mapa[decisao]
    if decisao == "pular":
        # 'pular' manda para o fim: zera o valor de ordenação sem perder o dado
        storage.adiar_item(item_id)
    else:
        storage.atualizar_dados_catalogo(
            item_id, status=status, erro=nota,
            decidido_em=__import__("datetime").datetime.now(
                __import__("datetime").timezone.utc).isoformat())

    return RedirectResponse(f"/fila/{lote_id}", status_code=303)


@app.get("/lotes/{lote_id}/exportar.csv")
async def exportar_lote(lote_id: int):
    """Exporta o lote em CSV — para analisar fora, ou mandar para quem ajuda."""
    if not storage.lote(lote_id):
        raise HTTPException(404, "Lote não encontrado.")

    colunas = ["linha", "sku", "descricao_erp", "marca", "quantidade", "preco",
               "status", "confianca", "catalog_product_id", "catalog_nome",
               "catalog_category_id", "ml_item_id", "permalink", "erro"]
    buf = io.StringIO()
    escritor = csv.DictWriter(buf, fieldnames=colunas, extrasaction="ignore")
    escritor.writeheader()
    for item in storage.itens_do_lote(lote_id):
        escritor.writerow({c: item[c] for c in colunas})

    return Response(
        # BOM para o Excel abrir com acento correto
        content="\ufeff" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="aguiahub-lote-{lote_id}.csv"'},
    )


@app.post("/itens/{item_id}/vincular")
async def vincular(request: Request, item_id: int, referencia: str = Form(...),
                   lote_id: int = Form(...)):
    """Campo único: aceita link da loja, link do ML, ou código solto."""
    try:
        resultado = await vincular_qualquer(item_id, referencia)
    except loja.LojaError as exc:
        raise HTTPException(400, str(exc))
    except CaminhoDoMLFechado as exc:
        # Beco sem saída de verdade: não adianta pedir outro link. A tela
        # mostra as duas saídas que funcionam, com botão.
        return templates.TemplateResponse(
            request, "sem_saida_ml.html",
            {"request": request, "mensagem": str(exc),
             "item": storage.item(item_id), "lote_id": lote_id},
            status_code=400)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except MLApiError as exc:
        raise HTTPException(400, f"Mercado Livre: {exc.mensagem_amigavel()}")
    if not resultado["ok"]:
        raise HTTPException(400, resultado["mensagem"])
    return RedirectResponse(f"/fila/{lote_id}", status_code=303)


@app.post("/itens/{item_id}/vincular-ml")
async def vincular_ml(item_id: int, referencia: str = Form(...),
                      lote_id: int = Form(...)):
    """Vincula manualmente um item a um produto de catálogo, pelo link do ML.

    Existe porque o ML bloqueou a busca pública de anúncios: o operador acha a
    peça no navegador, cola o link, e nós lemos o catalog_product_id da fonte.
    """
    if not storage.carregar_token():
        raise HTTPException(400, "Conecte a conta do Mercado Livre primeiro.")
    try:
        resultado = await vincular_por_link(item_id, referencia)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except MLApiError as exc:
        raise HTTPException(400, f"Mercado Livre: {exc.mensagem_amigavel()}")

    if not resultado["ok"]:
        raise HTTPException(400, resultado["mensagem"])
    return RedirectResponse(f"/lotes/{lote_id}/conferencia", status_code=303)


@app.post("/lotes/{lote_id}/publicar", response_class=HTMLResponse)
async def publicar(request: Request, lote_id: int,
                   aprovados: list[int] = Form(default=[]),
                   confirmacao: str = Form("")):
    """Publica de verdade — cria anúncios REAIS na conta da Águia Parts."""
    if not storage.carregar_token():
        raise HTTPException(400, "Conecte a conta do Mercado Livre primeiro.")
    # Confirmação digitada removida a pedido do João (v0.17.0).
    if not aprovados:
        raise HTTPException(400, "Nenhum item aprovado. Marque ao menos um.")

    quantos = storage.definir_aprovacao(lote_id, aprovados)
    log.info("Lote %s: publicando %s itens aprovados", lote_id, quantos)

    resultados = await publicar_aprovados(lote_id)
    storage.atualizar_status_lote(lote_id, "publicado")

    return templates.TemplateResponse(request, "resultado.html", {
        "request": request,
        "lote_id": lote_id,
        "resumo": resumir(resultados),
        "resultados": resultados,
    })


@app.get("/lotes/{lote_id}", response_class=HTMLResponse)
async def ver_lote(request: Request, lote_id: int):
    lote = storage.lote(lote_id)
    if not lote:
        raise HTTPException(404, "Lote não encontrado.")
    return templates.TemplateResponse(request, "lote.html", {
        "request": request,
        "lote": lote,
        "itens": storage.itens_do_lote(lote_id),
    })
