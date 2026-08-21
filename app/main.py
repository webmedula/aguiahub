"""Águiahub — publicação de anúncios no Mercado Livre a partir da planilha do ERP."""
from __future__ import annotations

import csv
import io
import logging
import secrets
from pathlib import Path

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
from app.ml.catalog import buscar_no_catalogo, escolher_publicavel
from app import loja
from app import fotos as mod_fotos
from app.publisher import (analisar_lote, cruzar_lote_com_loja,
                           publicar_aprovados, publicar_com_fotos_proprias,
                           publicar_da_loja, publicar_selecionados,
                           resumir, vincular_por_link, vincular_qualquer,
                           vincular_produto_da_loja)
from app.sheets import (FORMATOS_ACEITOS, FormatoNaoSuportado, calcular_preco,
                        ler_planilha, montar_titulo)
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

    for t in triados:
        storage.registrar_item(
            lote_id, t.item.linha, t.item.codigo, montar_titulo(t.item),
            payload={},
            status="na_fila" if t.pronto else "sem_dado",
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


@app.post("/lotes/{lote_id}/publicar-selecionadas", response_class=HTMLResponse)
async def publicar_selecionadas(request: Request, lote_id: int,
                                ids: list[int] = Form(default=[]),
                                confirmacao: str = Form("")):
    """Publica no Mercado Livre as peças marcadas. Cria anúncios REAIS."""
    if not storage.lote(lote_id):
        raise HTTPException(404, "Lote não encontrado.")
    if confirmacao.strip().upper() != "PUBLICAR":
        raise HTTPException(400, "Digite PUBLICAR para confirmar a publicação.")
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
    """Cria o anúncio no ML com as fotos da loja. Exige confirmação digitada."""
    if not storage.carregar_token():
        raise HTTPException(400, "Conecte a conta do Mercado Livre primeiro.")
    if confirmacao.strip().upper() != "PUBLICAR":
        raise HTTPException(400, (
            "Para publicar de verdade, digite PUBLICAR no campo de confirmação. "
            "Esta ação cria um anúncio real na conta da Águia Parts."))

    resultado = await publicar_da_loja(item_id)
    if not resultado["ok"]:
        raise HTTPException(400, resultado["mensagem"])
    return RedirectResponse(f"/fila/{lote_id}", status_code=303)


@app.post("/itens/{item_id}/fotos")
async def enviar_fotos(item_id: int, lote_id: int = Form(...),
                       arquivos: list[UploadFile] = File(...)):
    """Recebe as fotos tiradas pelo operador."""
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
    return RedirectResponse(f"/fila/{lote_id}", status_code=303)


@app.post("/itens/{item_id}/fotos/{nome}/apagar")
async def apagar_foto(item_id: int, nome: str, lote_id: int = Form(...)):
    try:
        mod_fotos.apagar(item_id, nome)
    except mod_fotos.FotoInvalida as exc:
        raise HTTPException(400, str(exc))
    return RedirectResponse(f"/fila/{lote_id}", status_code=303)


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
    if confirmacao.strip().upper() != "PUBLICAR":
        raise HTTPException(400, (
            "Para publicar de verdade, digite PUBLICAR no campo de confirmação. "
            "Esta ação cria um anúncio real na conta da Águia Parts."))
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
async def vincular(item_id: int, referencia: str = Form(...),
                   lote_id: int = Form(...)):
    """Campo único: aceita link da loja, link do ML, ou código solto."""
    try:
        resultado = await vincular_qualquer(item_id, referencia)
    except loja.LojaError as exc:
        raise HTTPException(400, str(exc))
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
    """Publica de verdade. Exige a palavra PUBLICAR digitada pelo operador."""
    if not storage.carregar_token():
        raise HTTPException(400, "Conecte a conta do Mercado Livre primeiro.")
    if confirmacao.strip().upper() != "PUBLICAR":
        raise HTTPException(
            400,
            "Confirmação incorreta. Digite PUBLICAR no campo para confirmar — "
            "esta ação cria anúncios reais na sua conta do Mercado Livre."
        )
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
