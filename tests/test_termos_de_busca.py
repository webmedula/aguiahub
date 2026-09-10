"""Testes dos termos de busca — v0.31.0.

O que motivou tudo isto foi uma peça real da lista da Águia:

    SERVO EMBREAGEM SCANIA SEM CABO / S2CP10055A
    Aplicação: ECA EMBREAGEM SCANIA SEM CABO REMAN 013317707R /
               COM CABO S2CP10087A

Ela voltava vazia da busca, e a resposta estava escrita na própria tela: os
dois códigos de referência cruzada no campo de aplicação. O sistema não
oferecia nenhum dos dois, e procurava só pelo `S2CP10055A`, que é interno.

Estes testes trancam três coisas:

* que os códigos escondidos em texto livre são achados, e que palavra comum
  não vira código por acidente;
* que a frase inteira vai para a busca ampla mas só o código vai para a busca
  interna dos sites — mandar frase para caixa de busca de loja devolve zero;
* que a segunda tentativa da esteira **não** acontece sem alguém pedir, porque
  cada tentativa extra é uma consulta paga multiplicada por 1.687 peças.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio                                            # noqa: E402

import app.config as cfg                                  # noqa: E402
from app import esteira, storage                          # noqa: E402
from app.busca import codigos_no_texto, separar_termo     # noqa: E402

APLICACAO = ("ECA EMBREAGEM SCANIA SEM CABO REMAN 013317707R / "
             "COM CABO S2CP10087A")


# --- codigos_no_texto -----------------------------------------------------

def test_acha_os_dois_codigos_da_peca_real():
    assert codigos_no_texto(APLICACAO) == ["013317707R", "S2CP10087A"]


def test_nao_devolve_o_codigo_do_proprio_item():
    """Oferecer o código que já foi tentado seria oferecer repetir o fracasso."""
    achados = codigos_no_texto(f"IGUAL A S2CP10055A / {APLICACAO}", "S2CP10055A")
    assert "S2CP10055A" not in achados
    assert achados == ["013317707R", "S2CP10087A"]


def test_palavra_comum_nao_vira_codigo():
    """EMBREAGEM, SCANIA e REMAN passariam por qualquer regex frouxa."""
    assert codigos_no_texto("ECA EMBREAGEM SCANIA SEM CABO REMAN") == []


def test_numero_curto_de_motor_nao_vira_codigo():
    """'MBB SW23 EURO 3 AP528' é descrição de aplicação, não part number."""
    assert codigos_no_texto("MODULO PLD MERCEDES MBB SW23 EURO 3 AP528") == []
    assert codigos_no_texto("SPRINTER 311 415 515 2.2 CDI") == []


def test_barra_separa_duas_referencias():
    assert codigos_no_texto("51258031008/51258201001") == \
        ["51258031008", "51258201001"]


def test_prefixo_grudado_nao_atrapalha():
    """Na planilha aparece 'ANTIGA Nº0445020606', com o símbolo colado."""
    assert codigos_no_texto("ANTIGA Nº0445020606") == ["0445020606"]


def test_codigo_repetido_aparece_uma_vez_so():
    assert codigos_no_texto("0445020007 e também 0445020007") == ["0445020007"]


# --- separar_termo --------------------------------------------------------

def test_frase_com_codigo_manda_so_o_codigo_para_os_sites():
    """A caixa de busca de uma loja devolve zero para frase com código no meio.

    Este é o ganho de v0.31.0 sobre a busca por nome da v0.29: antes o mesmo
    texto ia para os dois destinos, e nos sites cadastrados não achava nada.
    """
    para_site, para_api = separar_termo(
        "Servo Embreagem Scania Sem Cabo S2CP10055A")
    assert para_site == "S2CP10055A"
    assert para_api == "Servo Embreagem Scania Sem Cabo S2CP10055A"


def test_codigo_seco_vai_igual_para_os_dois():
    assert separar_termo("0281020067R") == ("0281020067R", "0281020067R")


def test_nome_sem_codigo_vai_igual_para_os_dois():
    """Sem código dentro não há o que separar — os dois recebem a frase."""
    assert separar_termo("Bomba Arla 32 Bosch") == \
        ("Bomba Arla 32 Bosch", "Bomba Arla 32 Bosch")


def test_termo_vazio_nao_quebra():
    assert separar_termo("") == ("", "")
    assert separar_termo("   ") == ("", "")


# --- a segunda tentativa da esteira ---------------------------------------

def _peca_sem_saida(tmp_path, monkeypatch):
    """Uma peça igual à real: código interno e dois códigos na aplicação."""
    s = cfg.get_settings()
    monkeypatch.setattr(s, "database_url", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setattr(s, "data_dir", str(tmp_path))
    monkeypatch.setattr(s, "fontes_imagem_autorizadas", "")
    storage.init_db()
    lote_id = storage.criar_lote("Itens parados.xls", 1, True, {})
    item_id = storage.registrar_item(
        lote_id, 42, "S2CP10055A", "SERVO EMBREAGEM SCANIA SEM CABO", {},
        status="na_fila", valor=14523.0, preco=14523.0, quantidade=1,
        descricao_erp="SERVO EMBREAGEM SCANIA SEM CABO",
        marca="FTE AUTOMOTIVE", aplicacao=APLICACAO)
    return lote_id, item_id


def _busca_que_nunca_acha(monkeypatch) -> list:
    """Registra cada consulta feita e devolve sempre vazio."""
    from app.busca import ResultadoBusca

    consultas: list[tuple[str, str]] = []

    async def falsa(codigo, contexto="", termo=""):
        consultas.append((codigo, termo))
        return ResultadoBusca(candidatos=[], avisos=["nada"])

    monkeypatch.setattr(esteira.busca, "buscar", falsa)
    return consultas


def test_sem_pedir_a_esteira_tenta_so_o_codigo(tmp_path, monkeypatch):
    """O padrão não pode gastar consulta paga que ninguém autorizou."""
    _lote, item_id = _peca_sem_saida(tmp_path, monkeypatch)
    consultas = _busca_que_nunca_acha(monkeypatch)

    asyncio.run(esteira._uma_peca(item_id))

    assert consultas == [("S2CP10055A", "")]


def test_pedindo_tenta_os_codigos_da_aplicacao_e_depois_o_nome(tmp_path,
                                                              monkeypatch):
    """A ordem é por precisão: código exato primeiro, nome só em último caso."""
    _lote, item_id = _peca_sem_saida(tmp_path, monkeypatch)
    consultas = _busca_que_nunca_acha(monkeypatch)

    asyncio.run(esteira._uma_peca(item_id, tentar_outros=True))

    assert consultas[0] == ("S2CP10055A", "")
    assert consultas[1] == ("013317707R", "")
    assert consultas[2] == ("S2CP10087A", "")
    # o nome vem por último, e como termo livre — não como código
    assert consultas[3][1].upper().startswith("SERVO EMBREAGEM")


def test_achando_no_primeiro_codigo_extra_nao_gasta_o_resto(tmp_path,
                                                            monkeypatch):
    """Consulta que não vai mudar a decisão é dinheiro jogado fora."""
    from app.busca import Candidato, ResultadoBusca

    _lote, item_id = _peca_sem_saida(tmp_path, monkeypatch)
    consultas: list = []

    async def falsa(codigo, contexto="", termo=""):
        consultas.append((codigo, termo))
        if codigo == "013317707R":
            return ResultadoBusca(candidatos=[Candidato(
                url="https://fornecedor-x.com.br/produto/servo",
                titulo="Servo Embreagem", confere_codigo=False)])
        return ResultadoBusca(candidatos=[], avisos=["nada"])

    monkeypatch.setattr(esteira.busca, "buscar", falsa)

    async def nao_abre(url):
        from app.extrator import ExtratorError
        raise ExtratorError("não consegui abrir")

    monkeypatch.setattr(esteira.extrator, "extrair", nao_abre)

    asyncio.run(esteira._uma_peca(item_id, tentar_outros=True))

    assert [c[0] for c in consultas] == ["S2CP10055A", "013317707R"]


def test_pagina_de_codigo_equivalente_nao_e_adotada_sozinha(tmp_path,
                                                            monkeypatch):
    """A trava mais importante desta versão.

    Achar a peça pelo código `013317707R` não autoriza usar a ficha dela para
    o item `S2CP10055A`: pode ser a versão remanufaturada, com foto e texto de
    outra coisa. O casamento continua exigindo o código do próprio item, então
    o resultado tem que ser CANDIDATOS — link guardado para a pessoa conferir,
    e nada aproveitado.
    """
    from app.busca import Candidato, ResultadoBusca
    from app.extrator import montar

    _lote, item_id = _peca_sem_saida(tmp_path, monkeypatch)

    pagina = """<html><head>
    <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Product",
     "name":"Servo Embreagem Scania Reman","sku":"013317707R",
     "description":"Servo de embreagem remanufaturado.",
     "image":["https://fornecedor-x.com.br/img/servo.jpg"]}
    </script></head><body></body></html>"""
    url = "https://fornecedor-x.com.br/produto/servo-013317707R"

    async def falsa(codigo, contexto="", termo=""):
        if codigo == "013317707R":
            return ResultadoBusca(candidatos=[Candidato(
                url=url, titulo="Servo Reman", confere_codigo=True)])
        return ResultadoBusca(candidatos=[], avisos=["nada"])

    async def extrai(alvo):
        return montar(pagina, alvo)

    monkeypatch.setattr(esteira.busca, "buscar", falsa)
    monkeypatch.setattr(esteira.extrator, "extrair", extrai)

    rotulo = asyncio.run(esteira._uma_peca(item_id, tentar_outros=True))

    assert rotulo == esteira.CANDIDATOS
    depois = storage.item(item_id)
    assert depois["status"] == "na_fila"
    assert not (depois["loja_fotos"] or "").strip("[] ")


# --- a tela de pré-anúncio ------------------------------------------------

def test_pre_anuncio_oferece_a_busca_com_os_codigos_da_aplicacao(tmp_path,
                                                                 monkeypatch):
    """O pedido do João: poder procurar imagem sem sair do pré-anúncio.

    E, junto, o que a peça real mostrou: os códigos da aplicação precisam
    estar ali como opção, não escondidos no meio do texto.
    """
    from fastapi.testclient import TestClient

    import app.main as main

    lote_id, item_id = _peca_sem_saida(tmp_path, monkeypatch)
    cli = TestClient(app=main.app, base_url="https://testserver")
    pagina = cli.get(f"/itens/{item_id}/preparar?lote_id={lote_id}").text

    assert "Procurar esta peça na internet" in pagina
    assert "nome + código" in pagina
    assert "013317707R" in pagina
    assert "S2CP10087A" in pagina
    # e a busca é a mesma rota de sempre, que não aproveita nada sozinha
    assert f"/itens/{item_id}/buscar-na-internet" in pagina


def test_voltar_da_busca_devolve_ao_pre_anuncio(tmp_path, monkeypatch):
    """Cair na tela da peça faria perder o texto que acabou de ser revisado."""
    from fastapi.testclient import TestClient

    from app.busca import ResultadoBusca

    import app.main as main

    lote_id, item_id = _peca_sem_saida(tmp_path, monkeypatch)

    async def falsa(codigo, contexto="", termo=""):
        return ResultadoBusca(candidatos=[], avisos=["nada"])

    monkeypatch.setattr(main.mod_busca, "buscar", falsa)

    cli = TestClient(app=main.app, base_url="https://testserver")
    pagina = cli.post(f"/itens/{item_id}/buscar-na-internet",
                      data={"lote_id": lote_id, "termo": "013317707R",
                            "volta_para": "preparar"}).text

    assert f"/itens/{item_id}/preparar?lote_id={lote_id}" in pagina


# --- diagnóstico da busca ampla (v0.31.1) ---------------------------------

def _resposta(status: int, corpo=None):
    import httpx

    pedido = httpx.Request("GET", "https://api.search.brave.com/res/v1/web/search")
    if corpo is None:
        return httpx.Response(status, request=pedido, text="erro")
    return httpx.Response(status, request=pedido, json=corpo)


def test_erro_da_api_diz_o_codigo_e_o_que_fazer():
    """"HTTPStatusError" e mais nada obrigava a adivinhar. Não pode voltar."""
    import httpx

    from app.busca import _erro_da_api

    exc = httpx.HTTPStatusError("", request=_resposta(422).request,
                                response=_resposta(422))
    recado = _erro_da_api("brave", exc)

    assert "422" in recado
    assert "recusou algum parâmetro" in recado
    # e continua dizendo que o resto do sistema não parou
    assert "sites cadastrados continua funcionando" in recado


def test_erro_de_chave_aponta_para_a_variavel_certa():
    import httpx

    from app.busca import _erro_da_api

    exc = httpx.HTTPStatusError("", request=_resposta(401).request,
                                response=_resposta(401))
    assert "BUSCA_API_KEY" in _erro_da_api("brave", exc)


def test_erro_repassa_o_codigo_proprio_da_provedora():
    """RATE_LIMITED/QUOTA_EXCEEDED é mais preciso que qualquer palpite meu."""
    import httpx

    from app.busca import _erro_da_api

    resp = _resposta(429, {"error": {"code": "RATE_LIMITED"}})
    exc = httpx.HTTPStatusError("", request=resp.request, response=resp)
    recado = _erro_da_api("brave", exc)

    assert "429" in recado
    assert "RATE_LIMITED" in recado


def test_a_brave_recebe_o_pais_em_maiuscula(monkeypatch):
    """A causa da falha real: `br` minúsculo devolve 422."""
    import httpx

    import app.busca as mod

    monkeypatch.setattr(cfg.get_settings(), "busca_api_key", "chave-de-teste")
    monkeypatch.setattr(cfg.get_settings(), "busca_api_provedor", "brave")
    visto: dict = {}

    class FalsoCliente:
        def __init__(self, *a, **k): pass

        async def __aenter__(self): return self

        async def __aexit__(self, *a): return False

        async def get(self, url, params=None, headers=None):
            visto["params"] = params or {}
            visto["headers"] = headers or {}
            return httpx.Response(200, request=httpx.Request("GET", url),
                                  json={"web": {"results": []}})

    monkeypatch.setattr(mod.httpx, "AsyncClient", FalsoCliente)
    asyncio.run(mod.buscar_por_api("0281020067"))

    assert visto["params"]["country"] == "BR"
    assert visto["params"]["search_lang"] == "pt"
    assert visto["headers"]["X-Subscription-Token"] == "chave-de-teste"
