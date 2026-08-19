"""Configuração da aplicação — tudo vem de variáveis de ambiente."""
import os
from functools import lru_cache


class Settings:
    # --- Mercado Livre ---
    ml_client_id: str = os.getenv("ML_CLIENT_ID", "")
    ml_client_secret: str = os.getenv("ML_CLIENT_SECRET", "")
    ml_redirect_uri: str = os.getenv(
        "ML_REDIRECT_URI",
        "https://31-97-87-68.sslip.io/oauth/mercadolivre/callback",
    )
    # Site do ML: MLB = Brasil
    ml_site_id: str = os.getenv("ML_SITE_ID", "MLB")
    # Domínio de autenticação varia por país (MLB -> .com.br)
    ml_auth_domain: str = os.getenv("ML_AUTH_DOMAIN", "auth.mercadolivre.com.br")
    ml_api_base: str = os.getenv("ML_API_BASE", "https://api.mercadolibre.com")
    # PKCE está desligado no DevCenter; ligue aqui se ativar lá
    ml_use_pkce: bool = os.getenv("ML_USE_PKCE", "false").lower() == "true"

    # --- Aplicação ---
    secret_key: str = os.getenv("SECRET_KEY", "troque-isto-em-producao")
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./data/aguiahub.db")
    data_dir: str = os.getenv("DATA_DIR", "./data")
    # Protege a interface. Se vazio, a tela fica aberta (não use assim em produção).
    app_password: str = os.getenv("APP_PASSWORD", "")

    # --- Comportamento de publicação ---
    # Quantos anúncios enviar em paralelo. O ML limita por app; 3 é conservador.
    publish_concurrency: int = int(os.getenv("PUBLISH_CONCURRENCY", "3"))
    dry_run_default: bool = os.getenv("DRY_RUN_DEFAULT", "true").lower() == "true"

    def validate(self) -> list[str]:
        problemas = []
        if not self.ml_client_id:
            problemas.append("ML_CLIENT_ID não definido")
        if not self.ml_client_secret:
            problemas.append("ML_CLIENT_SECRET não definido")
        if not self.ml_redirect_uri.startswith("https://"):
            problemas.append("ML_REDIRECT_URI precisa ser HTTPS")
        if self.secret_key == "troque-isto-em-producao":
            problemas.append("SECRET_KEY está no valor padrão")
        return problemas


@lru_cache
def get_settings() -> Settings:
    return Settings()
