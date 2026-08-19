"""Cliente HTTP do Mercado Livre com refresh automático e retry.

Cuidados embutidos:
- Renova o access_token antes de expirar e persiste o novo refresh_token
  (o do ML é de uso único — perder o novo obriga o seller a reautorizar).
- Faz backoff em 429 (rate limit) e em 5xx.
- Nunca loga o token.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.config import get_settings
from app.ml import oauth
from app import storage

log = logging.getLogger("aguiahub.ml")

_TENTATIVAS = 4
_lock = asyncio.Lock()

# Códigos que o ML devolve secos, sem explicação. Traduzir aqui poupa o operador
# de pesquisar o que significa e, principalmente, diz o que fazer a respeito.
ERROS_CONHECIDOS = {
    "address_pending":
        "a conta do Mercado Livre está sem endereço cadastrado. Entre no ML → "
        "Meu perfil → Endereços e cadastre o endereço de origem das vendas. "
        "Sem isso o ML não deixa publicar nenhum anúncio.",
    "is not active":
        "o produto de catálogo escolhido está inativo no Mercado Livre.",
    "item.category_id.invalid":
        "a categoria identificada não é válida para este produto.",
    "invalid_listing_type":
        "o tipo de anúncio não é aceito nesta categoria.",
    "price_invalid":
        "o preço está fora da faixa aceita pelo Mercado Livre para esta categoria.",
    "user_not_allowed":
        "a conta não tem permissão para publicar nesta categoria — pode faltar "
        "completar o cadastro de vendedor no ML.",
    "invalid_catalog_product":
        "o produto de catálogo não aceita novos anúncios.",
}


class MLApiError(RuntimeError):
    def __init__(self, status: int, corpo: Any, endpoint: str):
        self.status = status
        self.corpo = corpo
        self.endpoint = endpoint
        super().__init__(f"{endpoint} -> HTTP {status}: {corpo}")

    def mensagem_amigavel(self) -> str:
        """Traduz o erro do ML para algo que o operador entenda na tela."""
        bruto = str(self.corpo)
        for chave, texto in ERROS_CONHECIDOS.items():
            if chave in bruto:
                return texto

        corpo = self.corpo
        if isinstance(corpo, dict):
            causas = corpo.get("cause") or []
            if causas:
                partes = []
                for c in causas:
                    if isinstance(c, dict):
                        partes.append(c.get("message") or str(c))
                    else:
                        partes.append(str(c))
                return " | ".join(partes)
            if corpo.get("message"):
                return str(corpo["message"])
        return str(corpo)[:400]


class MLClient:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._bundle: oauth.TokenBundle | None = None
        self._nickname: str | None = None

    # -- token -------------------------------------------------------------

    def _carregar(self) -> None:
        if self._bundle is None:
            dados = storage.carregar_token()
            if dados is None:
                raise MLApiError(401, "Nenhuma conta do Mercado Livre conectada.", "auth")
            self._bundle, self._nickname = dados

    async def token_valido(self) -> str:
        self._carregar()
        assert self._bundle is not None
        if self._bundle.expirado:
            # lock evita duas renovações concorrentes queimarem o refresh_token
            async with _lock:
                dados = storage.carregar_token()
                if dados:
                    self._bundle, self._nickname = dados
                if self._bundle.expirado:
                    log.info("Renovando access_token do Mercado Livre")
                    novo = await oauth.renovar_token(self._bundle.refresh_token)
                    storage.salvar_token(novo, self._nickname)
                    self._bundle = novo
        return self._bundle.access_token

    @property
    def user_id(self) -> int:
        self._carregar()
        assert self._bundle is not None
        return self._bundle.user_id

    # -- requisições -------------------------------------------------------

    async def request(self, metodo: str, caminho: str, **kwargs) -> Any:
        s = self._settings
        url = caminho if caminho.startswith("http") else f"{s.ml_api_base}{caminho}"
        ultimo_erro: Exception | None = None

        for tentativa in range(_TENTATIVAS):
            token = await self.token_valido()
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                **kwargs.pop("headers", {}),
            }
            try:
                async with httpx.AsyncClient(timeout=45) as client:
                    resp = await client.request(metodo, url, headers=headers, **kwargs)
            except httpx.RequestError as exc:
                ultimo_erro = exc
                await asyncio.sleep(2 ** tentativa)
                continue

            if resp.status_code == 401 and tentativa == 0:
                # token pode ter sido invalidado do outro lado; força renovação
                if self._bundle:
                    self._bundle.expires_at = self._bundle.expires_at.replace(year=2000)
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                espera = int(resp.headers.get("Retry-After", 2 ** tentativa))
                log.warning("ML respondeu %s em %s; aguardando %ss",
                            resp.status_code, caminho, espera)
                await asyncio.sleep(min(espera, 30))
                ultimo_erro = MLApiError(resp.status_code, _corpo(resp), caminho)
                continue

            if resp.status_code >= 400:
                raise MLApiError(resp.status_code, _corpo(resp), caminho)

            return _corpo(resp)

        if isinstance(ultimo_erro, MLApiError):
            raise ultimo_erro
        raise MLApiError(0, f"Falha de rede após {_TENTATIVAS} tentativas: {ultimo_erro}",
                         caminho)

    async def get(self, caminho: str, **kw) -> Any:
        return await self.request("GET", caminho, **kw)

    async def post(self, caminho: str, json: dict, **kw) -> Any:
        return await self.request("POST", caminho, json=json, **kw)

    async def put(self, caminho: str, json: dict, **kw) -> Any:
        return await self.request("PUT", caminho, json=json, **kw)

    # -- atalhos -----------------------------------------------------------

    async def eu(self) -> dict:
        return await self.get("/users/me")

    async def diagnostico_conta(self) -> dict:
        """Confere se a conta está apta a publicar, ANTES de tentar.

        Evita queimar requisição (e tempo do operador) descobrindo item a item
        que a conta inteira está bloqueada. Em caso de dúvida devolve `apta`,
        para não travar a publicação por um falso negativo nosso.
        """
        info: dict = {"apta": True, "impedimentos": [], "nickname": None}
        try:
            eu = await self.eu()
        except MLApiError as exc:
            info["apta"] = False
            info["impedimentos"].append(f"não foi possível ler a conta: "
                                        f"{exc.mensagem_amigavel()}")
            return info

        info["nickname"] = eu.get("nickname")
        info["user_id"] = eu.get("id")

        # O ML exige endereço de origem para liberar publicação (address_pending)
        try:
            enderecos = await self.get(f"/users/{eu['id']}/addresses")
            if isinstance(enderecos, list) and not enderecos:
                info["apta"] = False
                info["impedimentos"].append(ERROS_CONHECIDOS["address_pending"])
        except MLApiError:
            pass       # sem permissão para ler endereços: seguimos em frente

        for restricao in (eu.get("status") or {}).get("list_restrictions") or []:
            info["apta"] = False
            info["impedimentos"].append(
                f"restrição do ML para anunciar: {restricao}")

        situacao = ((eu.get("status") or {}).get("site_status") or "").lower()
        if situacao and situacao != "active":
            info["apta"] = False
            info["impedimentos"].append(f"conta com status '{situacao}' no ML")

        return info

    async def tipos_de_anuncio(self) -> list[dict]:
        s = self._settings
        return await self.get(f"/sites/{s.ml_site_id}/listing_types")


def _corpo(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return resp.text[:1000]
