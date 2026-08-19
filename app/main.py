"""Águiahub — publicação de anúncios no Mercado Livre a partir da planilha do ERP."""
from __future__ import annotations

import logging
import secrets
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app import storage
from app.config import get_settings
from app.ml import oauth
from app.ml.client import MLClient, MLApiError
from app.publisher import processar_lote, resumir
from app.sheets import (FORMATOS_ACEITOS, FormatoNaoSuportado, calcular_preco,
                        ler_planilha, montar_titulo)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s — %(message)s")
log = logging.getLogger("aguiahub")

s = get_settings()
app = FastAPI(title="Águiahub", docs_url="/api/docs")
app.add_middleware(SessionMiddleware, secret_key=s.secret_key, https_only=True)

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))


@app.on_event("startup")
def _startup() -> None:
    storage.init_db()
    for problema in s.validate():
        log.warning("Configuração: %s", problema)


# ---------------------------------------------------------------------------
# Saúde e conexão
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    conta = storage.carregar_token()
    info = None
    if conta:
        bundle, nickname = conta
        info = {"user_id": bundle.user_id, "nickname": nickname,
                "expira_em": bundle.expires_at.isoformat()}
    return templates.TemplateResponse(request, "home.html", {
        "request": request,
        "conta": info,
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


@app.post("/planilha/processar", response_class=HTMLResponse)
async def processar(request: Request, arquivo_salvo: str = Form(...),
                    regra_preco: str = Form("preco_publico"),
                    percentual: float = Form(0.0),
                    dry_run: bool = Form(True),
                    abas: str = Form(""),
                    limite: int = Form(0)):
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

    lote_id = storage.criar_lote(arquivo_salvo, len(itens), dry_run,
                                 {"regra_preco": regra_preco, "percentual": percentual})
    resultados = await processar_lote(
        itens, regra_preco=regra_preco, percentual=percentual,
        dry_run=dry_run, lote_id=lote_id,
    )
    storage.atualizar_status_lote(lote_id, "simulado" if dry_run else "concluido")

    return templates.TemplateResponse(request, "resultado.html", {
        "request": request,
        "lote_id": lote_id,
        "dry_run": dry_run,
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
