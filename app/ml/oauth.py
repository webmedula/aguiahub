"""Fluxo OAuth 2.0 do Mercado Livre (Authorization Code + Refresh Token).

Notas importantes da API:
- access_token expira em 6 horas (expires_in = 21600)
- refresh_token é de USO ÚNICO: cada refresh devolve um novo, que precisa ser
  persistido. Se você perder o novo, o seller precisa reautorizar na mão.
- O refresh_token em si vale ~6 meses de validade.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from app.config import get_settings


@dataclass
class TokenBundle:
    access_token: str
    refresh_token: str
    user_id: int
    expires_at: datetime
    scope: str = ""

    @property
    def expirado(self) -> bool:
        # margem de 5 min para não usar token que morre no meio da requisição
        return datetime.now(timezone.utc) >= self.expires_at - timedelta(minutes=5)


def gerar_pkce() -> tuple[str, str]:
    """Retorna (code_verifier, code_challenge) no método S256."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def montar_url_autorizacao(state: str, code_challenge: str | None = None) -> str:
    """URL para onde o seller é enviado para autorizar o app."""
    s = get_settings()
    params = {
        "response_type": "code",
        "client_id": s.ml_client_id,
        "redirect_uri": s.ml_redirect_uri,
        "state": state,
    }
    if s.ml_use_pkce and code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    return f"https://{s.ml_auth_domain}/authorization?{urlencode(params)}"


async def _post_token(payload: dict) -> TokenBundle:
    s = get_settings()
    headers = {
        "accept": "application/json",
        "content-type": "application/x-www-form-urlencoded",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{s.ml_api_base}/oauth/token", data=payload, headers=headers
        )

    if resp.status_code >= 400:
        # A mensagem do ML costuma ser específica; propagar ajuda muito a depurar.
        raise OAuthError(
            f"Mercado Livre recusou a requisição de token "
            f"(HTTP {resp.status_code}): {resp.text}"
        )

    dados = resp.json()
    return TokenBundle(
        access_token=dados["access_token"],
        refresh_token=dados.get("refresh_token", ""),
        user_id=int(dados["user_id"]),
        expires_at=datetime.now(timezone.utc)
        + timedelta(seconds=int(dados.get("expires_in", 21600))),
        scope=dados.get("scope", ""),
    )


async def trocar_code_por_token(code: str, code_verifier: str | None = None) -> TokenBundle:
    s = get_settings()
    payload = {
        "grant_type": "authorization_code",
        "client_id": s.ml_client_id,
        "client_secret": s.ml_client_secret,
        "code": code,
        "redirect_uri": s.ml_redirect_uri,
    }
    if s.ml_use_pkce and code_verifier:
        payload["code_verifier"] = code_verifier
    return await _post_token(payload)


async def renovar_token(refresh_token: str) -> TokenBundle:
    s = get_settings()
    payload = {
        "grant_type": "refresh_token",
        "client_id": s.ml_client_id,
        "client_secret": s.ml_client_secret,
        "refresh_token": refresh_token,
    }
    return await _post_token(payload)


class OAuthError(RuntimeError):
    pass
