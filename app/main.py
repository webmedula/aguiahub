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
from app import busca as mod_busca
from app import cobertura as mod_cobertura
from app import esteira as mod_esteira
from app import painel as mod_painel
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
    # A esteira vive num task em memória. Se o container caiu no meio de uma
    # execução, a linha ficou marcada como 'rodando' e travaria a tela e o
    # botão de soltar outra. Aqui é o único momento em que dá para afirmar,
    # com certeza, que nenhuma esteira deste processo está rodando.
    orfas = storage.encerrar_execucoes_orfas() + storage.encerrar_medicoes_orfas()
    if orfas:
        log.warning("%s execução(ões) da esteira ficaram órfãs do container "
                    "anterior e foram encerradas", orfas)
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
    # Cai direto na lista: é dali que a pessoa enxerga o conjunto e escolhe.
    return RedirectResponse(f"/lotes/{lote_id}/lista", status_code=303)


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


# ---------------------------------------------------------------------------
# As três telas: a lista, a peça, o cartão de publicação
# ---------------------------------------------------------------------------

@app.get("/lotes/{lote_id}/lista", response_class=HTMLResponse)
async def lista(request: Request, lote_id: int, situacao: str = "",
                busca: str = "", faixa: str = "", pagina: int = 1):
    """A lista: todas as peças da planilha, com filtro e ação em bloco.

    Substitui a fila que mostrava uma peça por vez e escolhia a próxima pelo
    critério dela. Aqui quem escolhe é quem está olhando.
    """
    lote = storage.lote(lote_id)
    if not lote:
        raise HTTPException(404, "Lote não encontrado.")

    return templates.TemplateResponse(request, "lista.html", {
        "request": request, "lote": lote, "lote_id": lote_id,
        "contagem": mod_painel.contagem(lote_id),
        "situacoes": mod_painel.SITUACOES,
        "faixas": mod_painel.FAIXAS,
        "filtro": {"situacao": situacao, "busca": busca, "faixa": faixa},
        **mod_painel.listar(lote_id, situacao, busca, faixa, pagina),
    })


@app.get("/itens/{item_id}", response_class=HTMLResponse)
async def peca(request: Request, item_id: int, aviso: str = ""):
    """A peça: tudo o que dá para fazer com ela, num lugar só.

    Antes isto estava espalhado em três telas (fila, pesquisa, preparar) e a
    pessoa precisava saber qual botão levava onde.
    """
    linha = storage.item(item_id)
    if linha is None:
        raise HTTPException(404, "Peça não encontrada.")

    resumo = mod_painel.resumo_do_anuncio(item_id)
    return templates.TemplateResponse(request, "peca.html", {
        "request": request,
        "item": mod_painel.peca(item_id),
        "lote_id": linha["lote_id"],
        "resumo": resumo,
        "fotos_proprias": mod_fotos.listar(item_id),
        "fotos_site": json.loads(linha["loja_fotos"] or "[]"),
        "origens_de_foto": storage.origens_de_foto(item_id),
        "busca_pelo_nome": _termo_pelo_nome(linha),
        "situacoes": mod_painel.SITUACOES,
        "aviso": aviso,
    })


@app.post("/lotes/{lote_id}/publicar-lote", response_class=HTMLResponse)
async def conferir_publicacao(request: Request, lote_id: int,
                              ids: list[int] = Form(default=[])):
    """O cartão em bloco: mostra como cada anúncio vai ficar, antes de publicar.

    Este passo existe porque publicar é irreversível do lado de fora: o
    anúncio vai para a conta real da Águia Parts. Ver o que vai subir, com
    foto e preço, é o que transforma um clique arriscado num clique conferido.
    """
    if not storage.lote(lote_id):
        raise HTTPException(404, "Lote não encontrado.")
    if not ids:
        return RedirectResponse(f"/lotes/{lote_id}/lista?situacao=pronta",
                                status_code=303)

    resumos = [r for r in (mod_painel.resumo_do_anuncio(i) for i in ids) if r]
    return templates.TemplateResponse(request, "publicar.html", {
        "request": request, "lote_id": lote_id, "resumos": resumos,
        "total": sum(r["preco"] * r["quantidade"] for r in resumos),
        "resultado": None,
    })


@app.post("/lotes/{lote_id}/publicar-agora", response_class=HTMLResponse)
async def publicar_agora(request: Request, lote_id: int,
                         ids: list[int] = Form(default=[])):
    """Publica de verdade. Cria anúncios REAIS na conta da Águia Parts."""
    if not storage.lote(lote_id):
        raise HTTPException(404, "Lote não encontrado.")
    if not storage.carregar_token():
        raise HTTPException(400, "Conecte a conta do Mercado Livre primeiro.")

    resultado = await publicar_selecionados(ids)
    return templates.TemplateResponse(request, "publicar.html", {
        "request": request, "lote_id": lote_id, "resumos": [],
        "total": 0, "resultado": resultado,
    })


@app.get("/config", response_class=HTMLResponse)
async def configuracao(request: Request):
    """Tudo que se ajusta uma vez e sai da frente de quem só quer anunciar."""
    return templates.TemplateResponse(request, "config.html", {
        "request": request,
        "lotes": storage.listar_lotes(),
        "fontes": storage.listar_fontes(),
        "sites": storage.listar_sites_busca(),
        "do_ambiente": [d.strip() for d in
                        (s.fontes_imagem_autorizadas or "").split(",") if d.strip()],
        "conta": storage.carregar_token(),
        "problemas_config": s.validate(),
    })


@app.get("/lotes/{lote_id}/cobertura", response_class=HTMLResponse)
async def ver_cobertura(request: Request, lote_id: int):
    """Diagnóstico: de onde viria a foto de cada peça da fila.

    Só leitura. Nenhum status muda, nada é publicado — é para decidir onde
    investir esforço, e medição que altera o medido não decide nada.
    """
    if not storage.lote(lote_id):
        raise HTTPException(404, "Lote não encontrado.")

    medicao = storage.ultima_medicao(lote_id)
    resultado = None
    if medicao and medicao["resultado"]:
        resultado = json.loads(medicao["resultado"])

    return templates.TemplateResponse(request, "cobertura.html", {
        "request": request, "lote_id": lote_id, "medicao": medicao,
        "rodando": bool(medicao and medicao["status"] == "rodando"),
        "r": resultado,
        "fontes": sorted(fontes_permitidas()),
    })


@app.post("/lotes/{lote_id}/cobertura")
async def iniciar_cobertura(lote_id: int, amostra: int = Form(150)):
    if not storage.lote(lote_id):
        raise HTTPException(404, "Lote não encontrado.")
    if not storage.carregar_token():
        raise HTTPException(400, "Conecte a conta do Mercado Livre primeiro.")
    if not storage.medicao_rodando(lote_id):
        mod_cobertura.iniciar_em_segundo_plano(lote_id, amostra)
    return RedirectResponse(f"/lotes/{lote_id}/cobertura", status_code=303)


@app.get("/lotes/{lote_id}/esteira", response_class=HTMLResponse)
async def ver_esteira(request: Request, lote_id: int):
    """Tela da esteira: solta o processamento em massa e acompanha o andamento."""
    if not storage.lote(lote_id):
        raise HTTPException(404, "Lote não encontrado.")
    return templates.TemplateResponse(request, "esteira.html", {
        "request": request, "lote_id": lote_id,
        **mod_esteira.resumo_para_tela(lote_id),
    })


@app.post("/lotes/{lote_id}/esteira")
async def iniciar_esteira(lote_id: int, limite: int = Form(0),
                          reprocessar: str = Form(""),
                          tentar_outros: str = Form("")):
    """Solta a esteira no lote. Ela roda em segundo plano por horas.

    Não publica nada: termina em "pronta para publicar" e quem manda para o
    Mercado Livre continua sendo a pessoa, na tela de prontas.
    """
    if not storage.lote(lote_id):
        raise HTTPException(404, "Lote não encontrado.")
    if storage.esteira_rodando(lote_id):
        # Duas esteiras no mesmo lote dobrariam a bateção de porta nos sites
        # alheios e embaralhariam a contagem da tela.
        return RedirectResponse(f"/lotes/{lote_id}/esteira", status_code=303)

    mod_esteira.iniciar_em_segundo_plano(lote_id, limite, bool(reprocessar),
                                         bool(tentar_outros))
    return RedirectResponse(f"/lotes/{lote_id}/esteira", status_code=303)


@app.post("/lotes/{lote_id}/esteira/parar")
async def parar_esteira(lote_id: int):
    """Pede para a esteira parar — ela para entre uma peça e outra."""
    atual = storage.ultima_execucao(lote_id)
    if atual and atual["status"] in ("rodando", "parando"):
        storage.pedir_parada(atual["id"])
    return RedirectResponse(f"/lotes/{lote_id}/esteira", status_code=303)


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


@app.get("/sites", response_class=HTMLResponse)
async def sites(request: Request, erro: str = ""):
    """Onde o sistema procura a peça quando ninguém tem o link.

    Separado de /fontes de propósito: aqui é *onde procurar*, lá é *de quem
    a Águia tem direito de usar a foto*. Cadastrar um site aqui não autoriza
    imagem nenhuma.
    """
    return templates.TemplateResponse(request, "sites.html", {
        "request": request,
        "sites": storage.listar_sites_busca(),
        "autorizados": sorted(d for d in storage.dominios_autorizados()
                              if d != "*"),
        "api_ativa": bool((s.busca_api_key or "").strip()),
        "api_provedor": s.busca_api_provedor,
        "erro": erro,
    })


@app.post("/sites")
async def acrescentar_site(padrao: str = Form(...), nome: str = Form(""),
                           voltar_para: str = Form("")):
    try:
        storage.acrescentar_site_busca(padrao, nome)
    except ValueError as exc:
        return RedirectResponse(f"/sites?erro={quote(str(exc))}", status_code=303)
    return RedirectResponse(voltar_para or "/sites", status_code=303)


@app.post("/sites/remover")
async def remover_site(site_id: int = Form(...)):
    storage.remover_site_busca(site_id)
    return RedirectResponse("/sites", status_code=303)


@app.post("/sites/alternar")
async def alternar_site(site_id: int = Form(...)):
    storage.alternar_site_busca(site_id)
    return RedirectResponse("/sites", status_code=303)


@app.post("/itens/{item_id}/buscar-na-internet", response_class=HTMLResponse)
async def buscar_na_internet(request: Request, item_id: int,
                             lote_id: int = Form(...),
                             termo: str = Form(""),
                             volta_para: str = Form("")):
    """Procura a peça sozinho e mostra as páginas candidatas.

    Por padrão procura pelo código, que é a pista mais precisa. Mas nem todo
    código do ERP presta: o módulo PLD da lista tem um "código" de 22 dígitos,
    que não existe em site nenhum. Por isso dá para procurar por um termo
    livre — o nome da peça, normalmente —, e aí o casamento passa a ser por
    palavra em vez de por código exato.

    Nada é aproveitado automaticamente. Cada candidato leva para a mesma tela
    de ficha de sempre, onde a pessoa compara e decide.
    """
    linha = storage.item(item_id)
    if linha is None:
        raise HTTPException(404, "Peça não encontrada.")

    contexto = " ".join(t for t in (linha["descricao_erp"] or "",
                                    linha["marca"] or "") if t).strip()
    resultado = await mod_busca.buscar(linha["sku"] or "", contexto,
                                       termo=termo)

    return templates.TemplateResponse(request, "candidatos.html", {
        "request": request, "lote_id": lote_id, "item": linha,
        "resultado": resultado,
        "termo": (termo or "").strip() or (linha["sku"] or ""),
        "por_nome": bool((termo or "").strip()
                         and not mod_busca.e_codigo(termo)),
        "sugestao_nome": _termo_pelo_nome(linha),
        # De onde a pessoa veio, para o "voltar" devolver ela ao mesmo lugar.
        # Sem isso, quem procurava do pré-anúncio caía na tela da peça e
        # perdia o texto que tinha acabado de revisar.
        "voltar_para": (f"/itens/{item_id}/preparar?lote_id={lote_id}"
                        if volta_para == "preparar"
                        else f"/itens/{item_id}"),
    })


def _termo_pelo_nome(linha) -> str:
    """O nome da peça, do jeito que vale a pena procurar num site.

    Expande as abreviações do ERP ("BBA ARLA" vira "Bomba Arla 32") e junta a
    marca quando ela não é genérica — é o que dá alguma chance de casar com o
    título que a loja usa.
    """
    from app.abreviacoes import expandir
    from app.sheets import MARCAS_IGNORADAS

    partes = [expandir(linha["descricao_erp"] or "")]
    marca = (linha["marca"] or "").strip()
    if marca and marca.upper() not in MARCAS_IGNORADAS:
        partes.append(marca)
    return " ".join(p for p in partes if p).strip()[:80]


def _termos_de_busca(linha) -> list[dict]:
    """As formas de procurar esta peça, em ordem de chance de dar certo.

    O que motivou: o SERVO EMBREAGEM SCANIA (`S2CP10055A`) voltava vazio da
    busca, e a resposta estava na própria tela — o campo de aplicação dizia
    "REMAN 013317707R / COM CABO S2CP10087A". Dois códigos de referência
    cruzada, com muito mais chance de existir em catálogo do que o código do
    item, e o sistema não oferecia nenhum dos dois.

    Isto só *oferece* termos. Quem escolhe é a pessoa, e o aproveitamento de
    dado continua exigindo código exato lá no publisher.
    """
    codigo = (linha["sku"] or "").strip()
    nome = _termo_pelo_nome(linha)
    opcoes: list[dict] = []

    if nome and codigo:
        opcoes.append({
            "rotulo": "nome + código", "termo": f"{nome} {codigo}"[:120],
            "ajuda": "Costuma ser o melhor na busca ampla: dá duas pistas de "
                     "uma vez. Nos sites cadastrados só o código é enviado.",
        })
    if codigo:
        opcoes.append({
            "rotulo": "só o código", "termo": codigo,
            "ajuda": "O caminho mais preciso quando o código do ERP existe "
                     "no catálogo do fornecedor.",
        })
    if nome:
        opcoes.append({
            "rotulo": "só o nome", "termo": nome,
            "ajuda": "Para quando o código do ERP é interno e não existe em "
                     "site nenhum. Traz mais resultado e mais lixo junto.",
        })

    for outro in mod_busca.codigos_no_texto(linha["aplicacao"] or "", codigo):
        opcoes.append({
            "rotulo": outro, "termo": outro,
            "ajuda": "Código que estava escrito no campo de aplicação — "
                     "equivalente, substituto ou versão remanufaturada.",
        })
    return opcoes


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
        "termos_busca": _termos_de_busca(linha),
        # `s`, e não get_settings(), porque é o objeto que os testes trocam.
        "api_ativa": bool((s.busca_api_key or "").strip()),
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
    if volta_para == "peca":
        return f"/itens/{item_id}"
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

    # Foto tirada no estoque é o que faltava para a peça poder virar anúncio:
    # marca o status para ela aparecer como PRONTA na lista. Até a v0.26 o
    # status 'foto_do_operador' existia no código e nenhuma rota o gravava —
    # a peça ganhava foto e continuava contada como "sem foto".
    linha = storage.item(item_id)
    if linha is not None and linha["status"] == "na_fila":
        storage.atualizar_dados_catalogo(item_id, status="foto_do_operador")

    return RedirectResponse(_destino_fotos(item_id, lote_id, volta_para),
                            status_code=303)


@app.post("/itens/{item_id}/foto-por-url")
async def foto_por_url(item_id: int, endereco: str = Form(...),
                       lote_id: int = Form(...), volta_para: str = Form("peca")):
    """Baixa a imagem de um endereço colado e guarda como foto da peça.

    Existe porque o extrator nem sempre acha a foto sozinho, e a pessoa que
    está olhando a página acha em dois segundos: ela clica com o botão
    direito, copia o endereço da imagem e cola aqui.

    Decisão do João (v0.28.0): este caminho **baixa e registra a origem**, em
    vez de exigir o site autorizado antes. O registro é o que sobra para
    responder se um dia chegar reclamação — por isso guarda a URL exata, e
    não só o domínio. O botão de autorizar o site continua ao lado, para
    transformar a decisão desta foto em decisão para todas as próximas.
    """
    from app.extrator import dominio_de

    if storage.item(item_id) is None:
        raise HTTPException(404, "Peça não encontrada.")

    endereco = (endereco or "").strip()
    if not endereco.startswith(("http://", "https://")):
        raise HTTPException(400, (
            "Cole o endereço da imagem, começando com https:// — no navegador, "
            "clique com o botão direito na foto e escolha 'copiar endereço da "
            "imagem'."))

    try:
        conteudo = await loja.baixar_foto(endereco)
    except loja.LojaError as exc:
        raise HTTPException(400, str(exc))

    try:
        arquivo = mod_fotos.salvar(item_id, conteudo,
                                   loja.nome_do_arquivo(endereco, 0))
    except mod_fotos.FotoInvalida as exc:
        raise HTTPException(400, str(exc))

    storage.registrar_origem_de_foto(item_id, arquivo, endereco,
                                     dominio_de(endereco))

    linha = storage.item(item_id)
    if linha is not None and linha["status"] == "na_fila":
        storage.atualizar_dados_catalogo(item_id, status="foto_do_operador")

    return RedirectResponse(_destino_fotos(item_id, lote_id, volta_para),
                            status_code=303)


@app.post("/itens/{item_id}/fotos/{nome}/apagar")
async def apagar_foto(item_id: int, nome: str, lote_id: int = Form(...),
                      volta_para: str = Form("fila")):
    try:
        mod_fotos.apagar(item_id, nome)
    except mod_fotos.FotoInvalida as exc:
        raise HTTPException(400, str(exc))
    storage.esquecer_origem_de_foto(item_id, nome)

    # Apagou a última foto e não há foto de site: a peça deixa de estar pronta.
    # Sem isto ela continuaria na lista como "pronta" e falharia na publicação.
    linha = storage.item(item_id)
    if (linha is not None and linha["status"] == "foto_do_operador"
            and not mod_fotos.listar(item_id)
            and not json.loads(linha["loja_fotos"] or "[]")):
        storage.atualizar_dados_catalogo(item_id, status="na_fila")

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
