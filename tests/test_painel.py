"""Testes da lista, da peça e do cartão de publicação.

O que estes testes protegem é a promessa da tela: a situação de cada peça
tem que ser verdadeira, e o cartão tem que mostrar exatamente o que vai ser
publicado. Um cartão que mostra uma coisa e publica outra é pior do que não
ter cartão nenhum.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json                                               # noqa: E402

import app.config as cfg                                  # noqa: E402
from app import painel, storage                           # noqa: E402
from fastapi.testclient import TestClient                  # noqa: E402


def _lote(tmp_path, monkeypatch):
    s = cfg.get_settings()
    monkeypatch.setattr(s, "database_url", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setattr(s, "data_dir", str(tmp_path))
    storage.init_db()
    lote_id = storage.criar_lote("Itens parados.xls", 6, True, {})
    ids = {}

    ids["pronta"] = storage.registrar_item(
        lote_id, 2, "28447439", "BOMBA ALTA PRESSAO", {},
        status="pronto_com_fotos", preco=10318.72, quantidade=2, valor=20637.44,
        descricao_erp="BOMBA ALTA PRESSAO", marca="Delphi",
        loja_nome="Bomba Alta Pressão Sprinter 311 415 515",
        loja_fotos='["https://fornecedor-x.com.br/a.jpg"]')
    ids["no_ar"] = storage.registrar_item(
        lote_id, 3, "A2C59517051", "INJETOR", {}, status="publicado",
        preco=3188.83, quantidade=1, valor=3188.83, descricao_erp="INJETOR")
    ids["precisa"] = storage.registrar_item(
        lote_id, 4, "0281036486", "MODULO", {}, status="na_fila",
        preco=500.0, quantidade=1, valor=500.0, descricao_erp="MODULO")
    ids["sem_foto"] = storage.registrar_item(
        lote_id, 5, "CD110087", "PARAFUSO", {}, status="na_fila",
        preco=40.0, quantidade=3, valor=120.0, descricao_erp="PARAFUSO")
    ids["descartada"] = storage.registrar_item(
        lote_id, 6, "XYZ12345", "ANEL", {}, status="nao_encontrado",
        preco=90.0, quantidade=1, valor=90.0, descricao_erp="ANEL")
    ids["fora"] = storage.registrar_item(
        lote_id, 7, "", "SEM CODIGO", {}, status="sem_dado",
        preco=10.0, quantidade=1, valor=10.0, descricao_erp="SEM CODIGO")

    storage.marcar_esteira(ids["precisa"], "candidatos",
                           "3 páginas, código não bateu",
                           [{"url": "https://x.com/p", "titulo": "Módulo",
                             "dominio": "x.com", "confere": False}])
    return lote_id, ids


# ---------------------------------------------------------------------------
# A classificação — é o coração da tela
# ---------------------------------------------------------------------------

def test_cada_peca_cai_na_situacao_certa(tmp_path, monkeypatch):
    lote_id, ids = _lote(tmp_path, monkeypatch)
    situacao = {r["sku"]: r["situacao"]
                for r in painel.listar(lote_id)["linhas"]}

    assert situacao["28447439"] == painel.PRONTA
    assert situacao["A2C59517051"] == painel.NO_AR
    assert situacao["0281036486"] == painel.PRECISA
    assert situacao["CD110087"] == painel.SEM_FOTO
    assert situacao["XYZ12345"] == painel.DESCARTADA
    assert situacao[""] == painel.FORA


def test_contagem_bate_com_a_lista(tmp_path, monkeypatch):
    lote_id, _ = _lote(tmp_path, monkeypatch)
    contagem = painel.contagem(lote_id)

    assert contagem[painel.PRONTA]["itens"] == 1
    assert contagem[painel.SEM_FOTO]["itens"] == 1
    assert contagem[painel.PRONTA]["valor"] == 20637.44
    # toda peça aparece em exatamente uma situação
    assert sum(c["itens"] for c in contagem.values()) == 6


def test_foto_tirada_no_estoque_deixa_a_peca_pronta(tmp_path, monkeypatch):
    """Até a v0.26 o status 'foto_do_operador' existia e nenhuma rota o
    gravava: a peça ganhava foto e continuava contada como sem foto."""
    import app.main as main
    lote_id, ids = _lote(tmp_path, monkeypatch)
    cli = TestClient(main.app, base_url="https://testserver",
                     follow_redirects=False)

    r = cli.post(f"/itens/{ids['sem_foto']}/fotos",
                 data={"lote_id": lote_id, "volta_para": "peca"},
                 files={"arquivos": ("peca.gif", b"GIF89a" + b"\x00" * 20,
                                     "image/gif")})
    assert r.status_code == 303
    assert storage.item(ids["sem_foto"])["status"] == "foto_do_operador"

    linha = [l for l in painel.listar(lote_id)["linhas"]
             if l["sku"] == "CD110087"][0]
    assert linha["situacao"] == painel.PRONTA
    assert linha["origem_foto"] == "foto tirada no estoque"


def test_apagar_a_ultima_foto_tira_a_peca_de_pronta(tmp_path, monkeypatch):
    """Senão ela continuaria listada como pronta e falharia na publicação."""
    import app.main as main
    from app import fotos as mod_fotos
    lote_id, ids = _lote(tmp_path, monkeypatch)
    cli = TestClient(main.app, base_url="https://testserver",
                     follow_redirects=False)

    cli.post(f"/itens/{ids['sem_foto']}/fotos", data={"lote_id": lote_id},
             files={"arquivos": ("p.gif", b"GIF89a" + b"\x00" * 20, "image/gif")})
    nome = mod_fotos.listar(ids["sem_foto"])[0]
    cli.post(f"/itens/{ids['sem_foto']}/fotos/{nome}/apagar",
             data={"lote_id": lote_id})

    assert storage.item(ids["sem_foto"])["status"] == "na_fila"


# ---------------------------------------------------------------------------
# Filtros e paginação
# ---------------------------------------------------------------------------

def test_filtro_por_situacao(tmp_path, monkeypatch):
    lote_id, _ = _lote(tmp_path, monkeypatch)
    r = painel.listar(lote_id, situacao=painel.PRONTA)
    assert r["total"] == 1
    assert r["linhas"][0]["sku"] == "28447439"


def test_busca_acha_por_codigo_e_por_descricao(tmp_path, monkeypatch):
    lote_id, _ = _lote(tmp_path, monkeypatch)
    assert painel.listar(lote_id, busca="28447439")["total"] == 1
    assert painel.listar(lote_id, busca="parafuso")["total"] == 1
    assert painel.listar(lote_id, busca="nao existe")["total"] == 0


def test_filtro_por_faixa_de_preco(tmp_path, monkeypatch):
    lote_id, _ = _lote(tmp_path, monkeypatch)
    assert painel.listar(lote_id, faixa="alto")["total"] == 2      # 10318, 3188
    assert painel.listar(lote_id, faixa="baixo")["total"] == 3     # 40, 90, 10


def test_lista_vem_ordenada_por_valor_parado(tmp_path, monkeypatch):
    lote_id, _ = _lote(tmp_path, monkeypatch)
    valores = [l["valor"] for l in painel.listar(lote_id)["linhas"]]
    assert valores == sorted(valores, reverse=True)


def test_paginacao(tmp_path, monkeypatch):
    lote_id, _ = _lote(tmp_path, monkeypatch)
    monkeypatch.setattr(painel, "POR_PAGINA", 2)
    p1 = painel.listar(lote_id, pagina=1)
    assert len(p1["linhas"]) == 2 and p1["paginas"] == 3
    assert len(painel.listar(lote_id, pagina=3)["linhas"]) == 2


# ---------------------------------------------------------------------------
# O cartão
# ---------------------------------------------------------------------------

def test_cartao_mostra_o_que_sera_publicado(tmp_path, monkeypatch):
    lote_id, ids = _lote(tmp_path, monkeypatch)
    r = painel.resumo_do_anuncio(ids["pronta"])

    assert r["titulo"] == "Bomba Alta Pressão Sprinter 311 415 515"
    assert r["preco"] == 10318.72
    assert r["quantidade"] == 2
    assert r["imagens"] == ["https://fornecedor-x.com.br/a.jpg"]
    assert r["publicavel"] is True


def test_cartao_usa_o_titulo_editado(tmp_path, monkeypatch):
    lote_id, ids = _lote(tmp_path, monkeypatch)
    storage.atualizar_dados_catalogo(ids["pronta"],
                                     titulo_editado="Título do João")
    assert painel.resumo_do_anuncio(ids["pronta"])["titulo"] == "Título do João"


def test_cartao_avisa_quando_falta_foto(tmp_path, monkeypatch):
    lote_id, ids = _lote(tmp_path, monkeypatch)
    r = painel.resumo_do_anuncio(ids["sem_foto"])
    assert r["publicavel"] is False
    assert r["imagens"] == []


def test_cartao_e_a_publicacao_usam_o_mesmo_titulo(tmp_path, monkeypatch):
    """A promessa da tela: o cartão não pode mostrar um título e a publicação
    mandar outro. Os dois passam por _titulo_efetivo."""
    import inspect
    assert "_titulo_efetivo" in inspect.getsource(painel.resumo_do_anuncio)
    assert "_descricao_efetiva" in inspect.getsource(painel.resumo_do_anuncio)


# ---------------------------------------------------------------------------
# As telas abrem
# ---------------------------------------------------------------------------

def test_tela_da_lista_abre(tmp_path, monkeypatch):
    import app.main as main
    lote_id, _ = _lote(tmp_path, monkeypatch)
    cli = TestClient(main.app, base_url="https://testserver")
    r = cli.get(f"/lotes/{lote_id}/lista")
    assert r.status_code == 200
    assert "BOMBA ALTA PRESSAO" in r.text
    assert "prontas para publicar" in r.text


def test_tela_da_peca_abre_com_o_cartao(tmp_path, monkeypatch):
    import app.main as main
    lote_id, ids = _lote(tmp_path, monkeypatch)
    cli = TestClient(main.app, base_url="https://testserver")
    r = cli.get(f"/itens/{ids['pronta']}")
    assert r.status_code == 200
    assert "Como o anúncio vai ficar" in r.text
    assert "10318.72" in r.text or "10318,72" in r.text


def test_peca_publicada_mostra_o_comprovante(tmp_path, monkeypatch):
    import app.main as main
    lote_id, ids = _lote(tmp_path, monkeypatch)
    storage.atualizar_item(ids["no_ar"], status="publicado",
                           ml_item_id="MLB7477855204",
                           permalink="https://produto.mercadolivre.com.br/MLB-7477855204")
    cli = TestClient(main.app, base_url="https://testserver")
    r = cli.get(f"/itens/{ids['no_ar']}")
    assert "Anúncio no ar" in r.text
    assert "MLB7477855204" in r.text


def test_conferencia_antes_de_publicar_nao_publica(tmp_path, monkeypatch):
    """A tela de conferência é só conferência: não pode chamar publicação."""
    import app.main as main
    lote_id, ids = _lote(tmp_path, monkeypatch)

    def explode(*a, **kw):
        raise AssertionError("a conferência não pode publicar nada")

    monkeypatch.setattr(main, "publicar_selecionados", explode)
    cli = TestClient(main.app, base_url="https://testserver")
    r = cli.post(f"/lotes/{lote_id}/publicar-lote", data={"ids": [ids["pronta"]]})

    assert r.status_code == 200
    assert "Nada foi publicado ainda" in r.text
    assert "Publicar 1 anúncio(s)" in r.text


def test_publicar_sem_conta_conectada_e_recusado(tmp_path, monkeypatch):
    import app.main as main
    lote_id, ids = _lote(tmp_path, monkeypatch)
    storage.desconectar()
    cli = TestClient(main.app, base_url="https://testserver")
    r = cli.post(f"/lotes/{lote_id}/publicar-agora", data={"ids": [ids["pronta"]]})
    assert r.status_code == 400


def test_tela_de_configuracao_abre(tmp_path, monkeypatch):
    import app.main as main
    _lote(tmp_path, monkeypatch)
    cli = TestClient(main.app, base_url="https://testserver")
    r = cli.get("/config")
    assert r.status_code == 200
    assert "Marcas autorizadas" in r.text


def test_cartao_nao_anuncia_origem_de_foto_quando_nao_ha_foto(tmp_path,
                                                              monkeypatch):
    """A tela chegou a dizer "fotos da loja da Águia" logo acima de "Falta
    foto" — duas frases contraditórias na mesma tela."""
    lote_id, ids = _lote(tmp_path, monkeypatch)
    r = painel.resumo_do_anuncio(ids["sem_foto"])

    assert r["imagens"] == []
    assert r["origem_fotos"] == ""
    assert r["publicavel"] is False
