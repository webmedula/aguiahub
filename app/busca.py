"""Procurar a peça na internet a partir do código do ERP.

O que faltava: até a v0.23 o extrator (``app/extrator.py``) lia a ficha de
qualquer site — mas só depois que **alguém** tinha achado o link e colado na
tela. Achar o link era trabalho manual, peça por peça, num Google aberto ao
lado. Este módulo é a etapa anterior: dado o código da peça, ele devolve
*páginas candidatas* para o operador escolher.

Ele não decide nada. O que sai daqui é uma lista de links; quem confirma que
é a peça certa continua sendo a pessoa, na tela de ficha que já existe.

DUAS FONTES, NESTA ORDEM
------------------------

1. **Sites conhecidos** (padrão, sem chave nenhuma). Consulta a busca interna
   de cada site cadastrado — a mesma coisa que digitar o código no campo de
   busca dele. Entram aqui os sites cadastrados na tela ``/sites`` e, de
   graça, os domínios que já estão autorizados em ``/fontes``: se a Águia
   declarou ter direito sobre o material daquela marca, é justamente lá que
   vale procurar primeiro.

2. **API de busca** (opcional, desligada por padrão). Com ``BUSCA_API_KEY``
   configurada, consulta a Brave Search ou o Google Custom Search e alcança a
   internet inteira. Sem a chave, este caminho fica simplesmente inativo — o
   resto continua funcionando.

O QUE ESTE MÓDULO NÃO FAZ
-------------------------

Não raspa página de resultado do Google nem do Bing. Isso quebra sem aviso,
faz o IP do VPS ser bloqueado e vai contra os termos dos dois. Se um dia a
busca ampla for necessária, o caminho é a chave de API acima — o encaixe já
está pronto aqui.

Não baixa foto e não lê ficha: quem faz isso é o extrator, depois, na página
que a pessoa escolher. Aqui só se descobre *onde procurar*. As regras de
direito autoral (foto e texto só de fonte autorizada) continuam valendo lá,
intactas.

Respeita o ``robots.txt`` de cada site, igual ao extrator — é o sinal que o
site publica dizendo o que aceita que robô leia.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from urllib.parse import quote_plus, urljoin, urlparse, urlunparse

import httpx

from app.config import get_settings
from app.extrator import dominio_de, robots_permite
from app.loja import normalizar_codigo, variantes_de_codigo

#: Onde procurar num site de que só se sabe o domínio. São os endereços de
#: busca das plataformas de e-commerce mais comuns no Brasil, nesta ordem:
#: WooCommerce (o mais comum em autopeça), busca genérica de WordPress,
#: VTEX/Loja Integrada, e Magento. O primeiro que devolver candidato vence e
#: fica gravado — nas próximas peças aquele site custa uma requisição só.
PADROES_BUSCA = [
    "/?s={q}&post_type=product",
    "/?s={q}",
    "/busca?q={q}",
    "/search?q={q}",
    "/catalogsearch/result/?q={q}",
]

#: Trechos de caminho que denunciam página de produto.
_PISTAS_DE_PRODUTO = ("/produto/", "/produtos/", "/product/", "/products/",
                      "/peca/", "/pecas/", "/item/", "/p/", "/pd/")

#: Nada disso é página de produto — é a mecânica da loja.
_LIXO = ("/carrinho", "/cart", "/checkout", "/finalizar", "/minha-conta",
         "/my-account", "/wp-login", "/wp-admin", "/login", "/cadastro",
         "/categoria", "/category", "/categoria-produto", "/tag/", "/page/",
         "/feed", "/politica", "/privacidade", "/termos", "/contato",
         "/quem-somos", "/sobre", "/blog/", "/autor/", "/author/")

_EXTENSOES_ARQUIVO = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".pdf",
                      ".zip", ".css", ".js", ".xml", ".ico")

_CABECALHOS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 Aguiahub/1.0"),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "pt-BR,pt;q=0.9",
}

#: Teto de sites consultados numa busca. Existe para a tela não ficar minutos
#: pendurada quando a lista de sites crescer — e para não bater na porta de
#: dez sites por peça, com 1.687 peças pela frente.
MAX_SITES = 8
MAX_CANDIDATOS = 12


def e_codigo(termo: str) -> bool:
    """O termo é um part number, ou é nome de peça?

    Muda tudo no que vem depois: código se procura pelas variantes de
    pontuação (`0281036486` = `0.281.036.486`) e casa exato; nome se procura
    como está e casa por palavra. Tratar nome como código não acha nada, e
    tratar código como nome traz lixo.
    """
    limpo = (termo or "").strip()
    return bool(limpo) and " " not in limpo and any(c.isdigit() for c in limpo)


def _palavras(termo: str) -> set:
    """Palavras que valem para casar um nome de peça: as de 4 letras ou mais.

    'DE', 'PLD', 'MB' aparecem em qualquer página do site e só atrapalham a
    pontuação.
    """
    return {p for p in re.split(r"[^\wÀ-ÿ]+", (termo or "").upper())
            if len(p) >= 4}


@dataclass
class Candidato:
    """Uma página que *pode* ser a peça. Quem confirma é a pessoa."""
    url: str
    titulo: str = ""
    dominio: str = ""
    fonte: str = "site"          # 'site' (busca interna) ou 'api'
    confere_codigo: bool = False  # o código apareceu na URL ou no texto do link
    pontos: int = 0

    def __post_init__(self) -> None:
        if not self.dominio:
            self.dominio = dominio_de(self.url)


@dataclass
class ResultadoBusca:
    candidatos: list[Candidato] = field(default_factory=list)
    sites_consultados: list[str] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)
    api_ativa: bool = False


class BuscaError(RuntimeError):
    pass


# --- montagem da URL de busca ---------------------------------------------

def montar_url_busca(padrao: str, termo: str) -> str:
    """Monta o endereço de busca de um site.

    Aceita as duas formas que o operador pode cadastrar:

    - o endereço completo com ``{q}`` no lugar do termo
      (``https://site.com.br/busca?q={q}``);
    - só o domínio (``site.com.br``) — aí quem escolhe o caminho é
      ``PADROES_BUSCA``, tentando um de cada vez.
    """
    padrao = (padrao or "").strip()
    if not padrao:
        raise BuscaError("Site sem endereço de busca.")
    if not padrao.startswith(("http://", "https://")):
        padrao = "https://" + padrao.lstrip("/")
    if "{q}" not in padrao:
        raise BuscaError(
            f"O endereço '{padrao}' não tem {{q}} — sem isso o sistema não "
            "sabe onde encaixar o código da peça.")
    return padrao.replace("{q}", quote_plus(termo))


def caminhos_a_tentar(padrao: str) -> list[str]:
    """Endereços de busca a experimentar para um site cadastrado.

    Um site cadastrado com ``{q}`` tem endereço próprio e vira uma tentativa
    só. Um site cadastrado como domínio puro vira uma tentativa por padrão
    conhecido.
    """
    padrao = (padrao or "").strip().rstrip("/")
    if "{q}" in padrao:
        return [padrao]
    base = padrao if padrao.startswith(("http://", "https://")) \
        else "https://" + padrao.lstrip("/")
    return [base + p for p in PADROES_BUSCA]


# --- leitura da página de resultados --------------------------------------

_RE_LINK = re.compile(
    r'<a\b[^>]*\bhref=["\']([^"\'>]+)["\'][^>]*>(.*?)</a>', re.I | re.S)


def _limpar(bruto: str) -> str:
    import html as _html
    texto = re.sub(r"<[^>]+>", " ", bruto or "")
    return re.sub(r"\s+", " ", _html.unescape(texto)).strip()


def _sem_fragmento(url: str) -> str:
    partes = urlparse(url)
    return urlunparse(partes._replace(fragment=""))


def _descartar(caminho: str, url: str) -> bool:
    if any(caminho.endswith(ext) for ext in _EXTENSOES_ARQUIVO):
        return True
    if any(t in caminho for t in _LIXO):
        return True
    # ?add-to-cart=123 aponta para a mesma página com efeito colateral
    return "add-to-cart" in url or "add_to_cart" in url


def links_candidatos(html: str, base: str, termo: str,
                     limite: int = MAX_CANDIDATOS,
                     codigo: str = "") -> list[Candidato]:
    """Extrai da página de resultados os links que parecem ser a peça.

    Pura de propósito (não faz rede): é a parte que mais tem chance de errar,
    então precisa ser testável com HTML de verdade colado no teste.

    ``termo`` é o que foi procurado — pode ser o código ou o nome da peça.
    ``codigo`` é sempre o código do ERP, mesmo quando a busca foi por nome:
    é ele que decide a marca "o código aparece nesta página", e essa marca
    tem que continuar dizendo a verdade. Resultado de busca por nome nasce
    sem ela, e é assim mesmo — quem confirma a peça é a pessoa.
    """
    host = dominio_de(base)
    codigo = codigo or (termo if e_codigo(termo) else "")

    formas = {normalizar_codigo(f) for f in variantes_de_codigo(codigo)} \
        if codigo else set()
    formas.discard("")
    palavras = set() if e_codigo(termo) else _palavras(termo)

    achados: dict[str, Candidato] = {}
    for href, texto_bruto in _RE_LINK.findall(html or ""):
        href = href.strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue

        url = _sem_fragmento(urljoin(base, href))
        if not url.startswith(("http://", "https://")):
            continue

        # Só o próprio site. Página de busca é cheia de link para rede social,
        # plataforma de pagamento e afins — nada disso é a peça.
        alvo = dominio_de(url)
        if not (alvo == host or alvo.endswith("." + host)
                or host.endswith("." + alvo)):
            continue

        caminho = urlparse(url).path.lower()
        if not caminho.strip("/"):
            continue                     # a home do site
        if _descartar(caminho, url.lower()):
            continue

        texto = _limpar(texto_bruto)[:200]
        alvo_normalizado = normalizar_codigo(url)
        texto_normalizado = normalizar_codigo(texto)
        no_endereco = any(f in alvo_normalizado for f in formas)
        no_texto = any(f in texto_normalizado for f in formas)

        pontos = 0
        if no_endereco:
            pontos += 4
        if no_texto:
            pontos += 3

        if palavras:
            # Busca por nome: pontua por quantas palavras do termo aparecem.
            # Exigir metade evita que qualquer página do site entre só porque
            # tem uma palavra em comum.
            alvo_texto = f"{texto.upper()} {url.upper()}"
            achadas = sum(1 for p in palavras if p in alvo_texto)
            if achadas < max(2, (len(palavras) + 1) // 2):
                continue
            pontos += achadas

        if any(p in caminho for p in _PISTAS_DE_PRODUTO):
            pontos += 2
        if len(texto) >= 12:
            pontos += 1
        if pontos <= 0:
            continue

        anterior = achados.get(url)
        if anterior is None or pontos > anterior.pontos:
            achados[url] = Candidato(
                url=url, titulo=texto or url, dominio=alvo,
                confere_codigo=no_endereco or no_texto, pontos=pontos)
        elif texto and len(texto) > len(anterior.titulo or ""):
            # mesma URL vinda da imagem e do nome: fica com o texto melhor
            anterior.titulo = texto

    ordenados = sorted(achados.values(),
                       key=lambda c: (-c.pontos, len(c.titulo or "")))
    return ordenados[:limite]


# --- consulta de um site --------------------------------------------------

async def _baixar(cli: httpx.AsyncClient, url: str) -> str | None:
    if not await robots_permite(url):
        return None
    try:
        resp = await cli.get(url)
    except httpx.RequestError:
        return None
    if resp.status_code >= 400:
        return None
    if "html" not in resp.headers.get("content-type", "").lower():
        return None
    return resp.text


async def buscar_no_site(padrao: str, termo: str,
                         limite: int = MAX_CANDIDATOS,
                         codigo: str = "") -> tuple[list[Candidato], str]:
    """Procura o termo na busca interna de um site.

    Devolve ``(candidatos, padrao_que_funcionou)``. O segundo valor é o que
    permite gravar o endereço descoberto: da próxima peça em diante aquele
    site custa uma requisição em vez de cinco.
    """
    if e_codigo(termo):
        # inteiro, sem pontuação, agrupado
        formas = variantes_de_codigo(termo)[:3]
    else:
        formas = [termo.strip()] if (termo or "").strip() else []
    if not formas:
        return [], ""

    async with httpx.AsyncClient(timeout=15, follow_redirects=True,
                                 headers=_CABECALHOS) as cli:
        for tentativa in caminhos_a_tentar(padrao):
            for forma in formas:
                try:
                    url = montar_url_busca(tentativa, forma)
                except BuscaError:
                    break
                html = await _baixar(cli, url)
                if not html:
                    continue
                achados = links_candidatos(html, url, termo, limite,
                                           codigo=codigo)
                if achados:
                    return achados, tentativa
    return [], ""


# --- API de busca (pronta, ligada só com chave) ---------------------------

async def buscar_por_api(termo: str, contexto: str = "",
                         limite: int = MAX_CANDIDATOS) -> list[Candidato]:
    """Busca ampla via API. Sem chave configurada, devolve lista vazia.

    Duas provedoras porque as duas têm faixa gratuita que cobre o uso da
    Águia (Brave ~2.000 consultas/mês, Google CSE 100/dia) e nenhuma das duas
    exige contrato. A escolha é do dono: ``BUSCA_API_PROVEDOR``.
    """
    s = get_settings()
    chave = (s.busca_api_key or "").strip()
    if not chave:
        return []

    busca_completa = " ".join(t for t in (termo, contexto) if t).strip()
    provedor = (s.busca_api_provedor or "brave").strip().lower()

    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as cli:
            if provedor == "google":
                resp = await cli.get(
                    "https://www.googleapis.com/customsearch/v1",
                    params={"key": chave, "cx": s.busca_google_cx,
                            "q": busca_completa, "num": min(limite, 10)})
                resp.raise_for_status()
                itens = resp.json().get("items") or []
                brutos = [(i.get("link", ""), i.get("title", "")) for i in itens]
            else:
                resp = await cli.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    params={"q": busca_completa, "count": min(limite, 20),
                            "country": "br", "search_lang": "pt"},
                    headers={"Accept": "application/json",
                             "X-Subscription-Token": chave})
                resp.raise_for_status()
                web = (resp.json().get("web") or {}).get("results") or []
                brutos = [(r.get("url", ""), r.get("title", "")) for r in web]
    except (httpx.HTTPError, ValueError) as exc:
        raise BuscaError(
            f"A busca por API falhou ({type(exc).__name__}). A busca nos "
            "sites cadastrados continua funcionando.") from exc

    formas = ({normalizar_codigo(f) for f in variantes_de_codigo(termo)}
              if e_codigo(termo) else set())
    formas.discard("")

    saida: list[Candidato] = []
    for url, titulo in brutos:
        if not url.startswith(("http://", "https://")):
            continue
        # Anúncio de outro vendedor no ML não serve: o ML bloqueia a leitura
        # (403) e reaproveitar foto/texto de lá é o que derruba conta.
        if "mercadolivre" in url or "mercadolibre" in url:
            continue
        confere = any(f in normalizar_codigo(url) or f in normalizar_codigo(titulo)
                      for f in formas)
        saida.append(Candidato(url=url, titulo=titulo or url, fonte="api",
                               confere_codigo=confere, pontos=4 if confere else 1))
    saida.sort(key=lambda c: -c.pontos)
    return saida[:limite]


# --- orquestração ---------------------------------------------------------

def sites_para_consultar() -> list[dict]:
    """Onde procurar: os sites cadastrados mais os domínios já autorizados.

    Incluir as fontes de ``/fontes`` de graça é de propósito. Autorizar um
    domínio ali é a Águia dizendo "represento esta marca e tenho direito
    sobre o material dela" — é exatamente o site onde a peça tem mais chance
    de estar, e onde a foto poderá ser aproveitada depois.
    """
    from app import storage
    from app.extrator import fontes_permitidas

    saida: list[dict] = []
    vistos: set[str] = set()

    for linha in storage.listar_sites_busca(so_ativos=True):
        chave = (linha["padrao"] or "").lower()
        if not chave or chave in vistos:
            continue
        vistos.add(chave)
        saida.append({
            "id": linha["id"],
            "nome": linha["nome"] or linha["padrao"],
            # O endereço já descoberto vale mais que o domínio puro: com ele
            # a consulta é uma requisição em vez de até cinco tentativas.
            "padrao": linha["descoberto"] or linha["padrao"],
            "cadastrado_como": linha["padrao"],
            "origem": "cadastrado",
        })

    for dominio in sorted(fontes_permitidas()):
        # '*' autoriza imagem de qualquer fonte; não é um lugar onde procurar.
        if dominio == "*" or dominio.lower() in vistos:
            continue
        # Um domínio já cadastrado com endereço próprio não entra de novo
        if any(dominio in (s["padrao"] or "").lower() for s in saida):
            continue
        vistos.add(dominio.lower())
        saida.append({"id": None, "nome": dominio, "padrao": dominio,
                      "cadastrado_como": dominio,
                      "origem": "fonte autorizada"})

    return saida[:MAX_SITES]


async def buscar(codigo: str, contexto: str = "",
                 termo: str = "") -> ResultadoBusca:
    """Procura a peça em tudo que estiver disponível e devolve os candidatos.

    ``termo`` é o que vai ser procurado de fato. Vazio, procura pelo código —
    que é o caminho mais preciso quando o código presta. Preenchido, procura
    por aquilo: serve para o caso em que o código do ERP é inutilizável (o
    módulo PLD da lista tem um "código" de 22 dígitos, que não existe em site
    nenhum) e o nome da peça é a única pista que sobra.

    ``contexto`` é a descrição/marca do ERP — só a busca ampla por API usa,
    para desempatar ("0281036486 módulo injeção Bosch").
    """
    codigo = (codigo or "").strip()
    alvo = (termo or "").strip() or codigo
    resultado = ResultadoBusca()
    if not alvo:
        resultado.avisos.append(
            "Esta peça não tem código utilizável no ERP — e nenhum termo foi "
            "informado. Procure pelo nome da peça, ou use a foto da prateleira.")
        return resultado

    sites = sites_para_consultar()
    resultado.api_ativa = bool((get_settings().busca_api_key or "").strip())

    if not sites and not resultado.api_ativa:
        resultado.avisos.append(
            "Nenhum site cadastrado para procurar. Cadastre pelo menos um em "
            "Sites de busca — pode ser o site do fabricante da peça.")
        return resultado

    tarefas = [buscar_no_site(s["padrao"], alvo, codigo=codigo) for s in sites]
    if resultado.api_ativa:
        # na busca ampla, o contexto só ajuda quando o alvo é o código seco
        tarefas.append(buscar_por_api(alvo, contexto if alvo == codigo else ""))

    respostas = await asyncio.gather(*tarefas, return_exceptions=True)

    from app import storage

    todos: list[Candidato] = []
    for site, resposta in zip(sites, respostas):
        resultado.sites_consultados.append(site["nome"] or site["padrao"])
        if isinstance(resposta, BaseException):
            resultado.avisos.append(
                f"{site['nome'] or site['padrao']}: não deu para consultar "
                f"({type(resposta).__name__}).")
            continue
        achados, funcionou = resposta
        todos.extend(achados)
        # Guarda o endereço de busca que funcionou, para não redescobrir na
        # próxima peça. Só faz sentido para site cadastrado como domínio puro.
        if achados and funcionou and site["id"] and \
                "{q}" not in (site["cadastrado_como"] or ""):
            storage.gravar_padrao_descoberto(site["id"], funcionou)

    if resultado.api_ativa:
        da_api = respostas[-1]
        if isinstance(da_api, BaseException):
            resultado.avisos.append(str(da_api))
        else:
            todos.extend(da_api)

    # Dedupe entre sites: a mesma peça pode aparecer em dois lugares.
    unicos: dict[str, Candidato] = {}
    for c in todos:
        anterior = unicos.get(c.url)
        if anterior is None or c.pontos > anterior.pontos:
            unicos[c.url] = c

    resultado.candidatos = sorted(
        unicos.values(), key=lambda c: (not c.confere_codigo, -c.pontos))[:MAX_CANDIDATOS]

    if not resultado.candidatos:
        como = "o código" if alvo == codigo else "o termo"
        resultado.avisos.append(
            f"Nenhum dos sites consultados devolveu página com {como} "
            f"\u201c{alvo}\u201d. Isso não quer dizer que a peça não exista — quer "
            "dizer que ela não está, com esse termo, nos sites que o sistema "
            "conhece."
            + ("" if alvo != codigo else
               " Vale tentar de novo procurando pelo nome da peça."))
    return resultado
