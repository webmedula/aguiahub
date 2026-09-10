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
         "/quem-somos", "/sobre", "/blog/", "/autor/", "/author/",
         # institucional e conta, que apareceram de verdade como "candidatos"
         "/meus-pedidos", "/meu-pedido", "/como-comprar", "/atendimento",
         "/entrega", "/frete", "/rastrei", "/troca", "/devolucao", "/garantia",
         "/faq", "/duvidas", "/fale-conosco", "/institucional", "/empresa",
         "/trabalhe", "/lojas", "/newsletter", "/departamento", "/marcas",
         "/linha/", "/segmento")

#: O TEXTO do link também denuncia menu. "Bomba de Água" e "Reparo Injetor"
#: são categorias da bulltechdiesel que entraram como candidatos numa busca
#: que não achou nada — o site devolveu a página de "nenhum resultado" e o
#: sistema raspou o menu dela.
_LIXO_NO_TEXTO = (
    "meus pedidos", "meu pedido", "como comprar", "quem somos", "fale conosco",
    "atendimento", "política", "politica", "privacidade", "termos", "trocas",
    "devolução", "devolucao", "garantia", "frete", "rastrear", "rastreio",
    "entre em contato", "cadastre-se", "minha conta", "criar conta", "login",
    "carrinho", "newsletter", "trabalhe conosco", "todas as categorias",
    "ver todos", "ver mais", "leia mais", "saiba mais", "página inicial",
    "pagina inicial", "início", "inicio", "home",
)

#: A página de resultado vazio se anuncia. Reconhecer isso é melhor do que
#: deixar o filtro adivinhar pelo que sobrou no menu.
_SEM_RESULTADO = (
    "nenhum resultado", "nenhum produto encontrado", "nenhum produto foi",
    "não encontramos", "nao encontramos", "sua busca não retornou",
    "sua busca nao retornou", "não foram encontrados", "nao foram encontrados",
    "no products were found", "nada foi encontrado", "sem resultados",
    "0 produtos encontrados", "nenhum item encontrado",
)

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


#: Um part number solto no meio de texto livre. Aceita começar por letra
#: (`S2CP10087A` é código de verdade), mas exige massa de dígito — é o que
#: separa código de palavra: "EMBREAGEM" e "SCANIA" não têm nenhum.
_RE_CODIGO_SOLTO = re.compile(r"[A-Z0-9][A-Z0-9\-.]{4,}")

#: Palavras que passam no teste de dígito mas não são peça nenhuma.
_NAO_E_CODIGO = ("EURO", "REMAN", "ANOS", "MOTOR", "SERIE", "MODELO")


def codigos_no_texto(texto: str, ignorar: str = "") -> list[str]:
    """Os part numbers escondidos no texto livre do ERP.

    O campo de aplicação da planilha guarda referência cruzada em prosa::

        ECA EMBREAGEM SCANIA SEM CABO REMAN 013317707R / COM CABO S2CP10087A

    Ali dentro estão os dois códigos que têm chance real de existir em
    catálogo de fornecedor — enquanto o código do próprio item (`S2CP10055A`)
    pode não existir em lugar nenhum. Até a v0.30 esses códigos eram ignorados,
    e era por isso que peça assim voltava da busca sem nada.

    Não decide nada: devolve termos para *oferecer* a quem está na tela. O
    casamento de peça continua sendo por código exato, no publisher.
    """
    achados: list[str] = []
    alvo_ignorar = normalizar_codigo(ignorar)

    # A barra separa referências ("51258031008/51258201001"), então ela é
    # divisor de token, não parte do código.
    for bruto in re.split(r"[\s/;,()]+", (texto or "").upper()):
        for token in _RE_CODIGO_SOLTO.findall(bruto):
            limpo = token.strip(".-")
            digitos = sum(c.isdigit() for c in limpo)
            # 4 dígitos é o piso: pega `S2CP10087A` (6) e barra `SW23` (2).
            if digitos < 4 or len(limpo) < 6 or limpo in _NAO_E_CODIGO:
                continue
            if normalizar_codigo(limpo) == alvo_ignorar:
                continue
            if limpo not in achados:
                achados.append(limpo)
    return achados


def separar_termo(alvo: str) -> tuple[str, str]:
    """O mesmo termo, na forma que cada destino aceita.

    Devolve ``(para_site, para_api)``. São diferentes de propósito:

    * A **busca ampla por API** vai melhor com nome e código na mesma
      consulta — "Servo Embreagem Scania Sem Cabo S2CP10055A" dá dois sinais
      ao buscador e desempata sozinha.
    * A **busca interna dos sites** vai pior com isso. Caixa de busca de loja
      quase sempre devolve zero quando recebe frase com código no meio: ela
      quer o código sozinho. Então quando a frase tem um código dentro, é só
      ele que vai para os sites.

    Mandar a mesma coisa para os dois era o que fazia a busca por nome (v0.29)
    render menos do que devia nos sites cadastrados.
    """
    frase = (alvo or "").strip()
    if not frase or e_codigo(frase):
        return frase, frase
    dentro = codigos_no_texto(frase)
    return (dentro[0] if dentro else frase), frase


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


def _parece_peca(titulo: str) -> bool:
    """O título é de uma peça, ou de uma categoria do menu?

    "Sensor de Rotação Bosch 0261210170" é peça: tem código.
    "Capa De Protecao Bosch Lacre Usos Diversos" é peça: é longo.
    "Bomba de Água", "Canos e Tubos", "Reparo Injetor" são categorias.
    """
    texto = (titulo or "").strip()
    if any(c.isdigit() for c in texto):
        return True
    return len(texto.split()) >= 4


def pagina_sem_resultado(html: str) -> bool:
    """A página de busca está dizendo, com todas as letras, que não achou nada."""
    texto = _limpar(html or "").lower()
    return any(frase in texto for frase in _SEM_RESULTADO)


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

    # O site já disse que não achou nada. Vasculhar essa página só produz
    # falso positivo — é dela que saíam os "candidatos" que eram menu.
    if pagina_sem_resultado(html):
        return []

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

        # Texto de menu não é produto, por mais bonito que seja o endereço.
        if any(l in texto.lower() for l in _LIXO_NO_TEXTO):
            continue

        cara_de_produto = any(p in caminho for p in _PISTAS_DE_PRODUTO)

        pontos = 0
        if no_endereco:
            pontos += 4
        if no_texto:
            pontos += 3
        if cara_de_produto:
            pontos += 2

        if palavras:
            # Busca por nome: pontua por quantas palavras do termo aparecem.
            # Exigir metade evita que qualquer página do site entre só porque
            # tem uma palavra em comum.
            alvo_texto = f"{texto.upper()} {url.upper()}"
            achadas = sum(1 for p in palavras if p in alvo_texto)
            if achadas < max(2, (len(palavras) + 1) // 2):
                continue
            pontos += achadas

        # SINAL REAL para entrar na lista: o código apareceu, ou o endereço
        # tem cara de página de produto, ou (na busca por nome) as palavras
        # casaram. Antes bastava o texto do link ter 12 caracteres — um
        # desempate que virou critério de entrada por descuido, e enchia a
        # tela com o menu do site sempre que a busca não achava nada:
        # "Meus pedidos", "Como comprar", "Bomba de Água"...
        if pontos <= 0:
            continue

        # daqui para baixo é só desempate entre candidatos que já entraram
        if len(texto) >= 12:
            pontos += 1

        anterior = achados.get(url)
        if anterior is None or pontos > anterior.pontos:
            achados[url] = Candidato(
                url=url, titulo=texto or url, dominio=alvo,
                confere_codigo=no_endereco or no_texto, pontos=pontos)
        elif texto and len(texto) > len(anterior.titulo or ""):
            # mesma URL vinda da imagem e do nome: fica com o texto melhor
            anterior.titulo = texto

    # Categoria não é peça — e mora no mesmo lugar que ela ("/produtos/bomba-de-agua"
    # e "/produto/capa-de-protecao-2420580001" têm a mesma cara). O que separa
    # as duas é o TÍTULO: nome de peça carrega código ou é longo; nome de
    # categoria são duas ou três palavras genéricas, sem número nenhum.
    #
    # Peça equivalente do mesmo site continua entrando de propósito (ela é
    # útil, e a tela já marca que o código não bate) — só o menu sai.
    encontrados = [c for c in achados.values()
                   if c.confere_codigo or _parece_peca(c.titulo)]

    ordenados = sorted(encontrados,
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

#: O que cada código HTTP quer dizer, em português e com o que fazer. Existe
#: porque a primeira busca real da Águia falhou com "HTTPStatusError" e mais
#: nada — mensagem que não diz se o problema é a chave, o crédito ou um
#: parâmetro, e obriga a adivinhar. O erro precisa dizer o que fazer.
_RECADOS_HTTP = {
    401: ("A chave da busca não foi aceita. Confira BUSCA_API_KEY nas "
          "variáveis do EasyPanel — é a chave da API, não a senha da conta."),
    402: ("O crédito da busca acabou. A Brave cobra por consulta desde "
          "12/02/2026; veja o saldo no painel dela."),
    403: ("A chave não tem permissão para este endpoint. Confira se ela foi "
          "criada para o plano de busca web."),
    422: ("A Brave recusou algum parâmetro da consulta. Se acabou de "
          "atualizar o sistema, é provável que a versão antiga ainda esteja "
          "rodando: a v0.31.1 corrigiu o código de país, que ia minúsculo."),
    429: ("Consultas demais em pouco tempo. A esteira segura sozinha; se "
          "isto aparecer com o sistema parado, o limite do plano estourou."),
}


def _erro_da_api(provedor: str, exc: "httpx.HTTPStatusError") -> str:
    """Mensagem de falha que diz o código, a causa provável e o que fazer."""
    codigo = exc.response.status_code
    recado = _RECADOS_HTTP.get(codigo, "")

    # A Brave devolve um código próprio no corpo (RATE_LIMITED, QUOTA_...),
    # que é mais preciso que o HTTP. Vale mais que qualquer palpite meu.
    detalhe = ""
    try:
        corpo = exc.response.json()
        erro = corpo.get("error") if isinstance(corpo, dict) else None
        if isinstance(erro, dict):
            detalhe = str(erro.get("code") or erro.get("detail") or "")[:120]
        elif erro:
            detalhe = str(erro)[:120]
    except ValueError:
        detalhe = (exc.response.text or "")[:120].replace("\n", " ")

    partes = [f"A busca ampla ({provedor}) respondeu HTTP {codigo}."]
    if recado:
        partes.append(recado)
    if detalhe:
        partes.append(f"A provedora disse: {detalhe}")
    partes.append("A busca nos sites cadastrados continua funcionando.")
    return " ".join(partes)

async def buscar_por_api(termo: str, contexto: str = "",
                         limite: int = MAX_CANDIDATOS) -> list[Candidato]:
    """Busca ampla via API. Sem chave configurada, devolve lista vazia.

    Duas provedoras, e nenhuma exige contrato. A escolha é do dono, em
    ``BUSCA_API_PROVEDOR``.

    Custo, conferido em setembro de 2026: a Brave encerrou em 12/02/2026 o
    plano gratuito de 2.000 consultas/mês e passou a cobrar por consulta —
    US$ 5 por mil, com US$ 5 de crédito mensal (~1.000 consultas). O Google
    CSE segue com 100/dia grátis. Uma varredura das 1.687 peças custa uns
    US$ 8 na Brave, menos o crédito. O limite de vazão dela é 50 consultas
    por segundo, bem acima da concorrência da esteira — não precisa de
    controle de vazão aqui.
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
                    # `country` em MAIÚSCULA: a Brave documenta ISO 3166-1
                    # alpha-2 e devolve 422 com "br" minúsculo. Foi o que
                    # derrubou a primeira busca real da Águia (v0.31.0).
                    # `search_lang`, ao contrário, é minúsculo.
                    params={"q": busca_completa, "count": min(limite, 20),
                            "country": "BR", "search_lang": "pt"},
                    headers={"Accept": "application/json",
                             "Accept-Encoding": "gzip",
                             "X-Subscription-Token": chave})
                resp.raise_for_status()
                web = (resp.json().get("web") or {}).get("results") or []
                brutos = [(r.get("url", ""), r.get("title", "")) for r in web]
    except httpx.HTTPStatusError as exc:
        raise BuscaError(_erro_da_api(provedor, exc)) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise BuscaError(
            f"A busca por API não respondeu ({type(exc).__name__}). A busca "
            "nos sites cadastrados continua funcionando.") from exc

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

    # Cada destino recebe a forma que ele aceita: código seco para a busca
    # interna dos sites, frase inteira para a busca ampla. Ver separar_termo().
    para_site, para_api = separar_termo(alvo)

    tarefas = [buscar_no_site(s["padrao"], para_site, codigo=codigo)
               for s in sites]
    if resultado.api_ativa:
        # na busca ampla, o contexto só ajuda quando o alvo é o código seco
        tarefas.append(
            buscar_por_api(para_api, contexto if para_api == codigo else ""))

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
