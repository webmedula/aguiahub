"""Testes do payload enviado ao Mercado Livre."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.publisher import montar_payload            # noqa: E402
from app.sheets import LinhaProduto                 # noqa: E402


def item():
    return LinhaProduto(aba="t", linha=1, codigo="A2C59517051",
                        descricao="BICO INJETOR DIESEL", quantidade=19,
                        marca="CONTINENTAL")


def test_catalogo_envia_category_id():
    """Regressão: o ML recusa o POST sem category_id, mesmo por catálogo.

    Erro real observado em produção (lote #3):
    'The body does not contains some or none of the following properties
     [category_id]'
    """
    p = montar_payload(item(), 3188.83, catalog_product_id="MLB123",
                       category_id="MLB1747")
    assert p["category_id"] == "MLB1747"
    assert p["catalog_product_id"] == "MLB123"
    assert p["catalog_listing"] is True


def test_catalogo_nao_envia_titulo_nem_atributos():
    """São herdados do catálogo; mandar os nossos gera conflito de validação."""
    p = montar_payload(item(), 100.0, catalog_product_id="MLB123",
                       category_id="MLB1747")
    assert "title" not in p
    assert "attributes" not in p
    assert p["pictures"] == []


def test_anuncio_proprio_leva_titulo_e_atributos():
    p = montar_payload(item(), 100.0, category_id="MLB1747")
    assert p["title"]
    ids = {a["id"] for a in p["attributes"]}
    assert {"BRAND", "PART_NUMBER", "SELLER_SKU"} <= ids


def test_campos_obrigatorios_sempre_presentes():
    p = montar_payload(item(), 3188.83, catalog_product_id="MLB123",
                       category_id="MLB1747")
    for campo in ("site_id", "price", "currency_id", "available_quantity",
                  "buying_mode", "condition", "listing_type_id", "category_id"):
        assert campo in p, f"faltou {campo}"
    assert p["condition"] == "new"          # catálogo só aceita novo
    assert p["currency_id"] == "BRL"
    assert p["available_quantity"] == 19


def test_quantidade_zero_vira_um():
    p = LinhaProduto(aba="t", linha=1, codigo="X", descricao="Y", quantidade=0)
    assert montar_payload(p, 10.0)["available_quantity"] == 1


# --- elegibilidade do produto de catálogo ---------------------------------

from app.ml.catalog import CandidatoCatalogo, publicavel   # noqa: E402


def cand(**kw):
    base = dict(catalog_product_id="MLB43115763", nome="4 injetores Continental",
                domain_id="MLB-INJECTORS", status="active", confianca="alta",
                motivo="part number exato", category_id="MLB1747")
    base.update(kw)
    return CandidatoCatalogo(**base)


def test_produto_ativo_com_categoria_e_publicavel():
    ok, motivo = publicavel(cand())
    assert ok and motivo == ""


def test_produto_inativo_nao_e_publicavel():
    """Regressão: o POST devolvia 'Product MLB43115763 is not active'.

    O filtro status=active da busca não é confiável — o status do detalhe é.
    """
    ok, motivo = publicavel(cand(status="inactive"))
    assert not ok
    assert "inactive" in motivo


def test_produto_sem_status_nao_e_publicavel():
    ok, _ = publicavel(cand(status=None))
    assert not ok


def test_produto_sem_categoria_nao_e_publicavel():
    ok, motivo = publicavel(cand(category_id=""))
    assert not ok
    assert "ategoria" in motivo


# --- tradução de erros do Mercado Livre -----------------------------------

from app.ml.client import MLApiError                        # noqa: E402


def test_address_pending_vira_instrucao_acionavel():
    """Regressão: o ML devolve só 'address_pending', sem dizer o que fazer."""
    erro = MLApiError(400, {"message": "address_pending"}, "/items")
    msg = erro.mensagem_amigavel()
    assert "endereço" in msg.lower()
    assert "Meu perfil" in msg


def test_produto_inativo_traduzido():
    erro = MLApiError(400, {"message": "Product MLB43115763 is not active"}, "/items")
    assert "inativo" in erro.mensagem_amigavel()


def test_erro_desconhecido_mantem_texto_original():
    erro = MLApiError(400, {"message": "algo totalmente novo"}, "/items")
    assert "algo totalmente novo" in erro.mensagem_amigavel()


def test_causas_do_ml_sao_concatenadas():
    erro = MLApiError(400, {"cause": [{"message": "campo A inválido"},
                                      {"message": "campo B inválido"}]}, "/items")
    msg = erro.mensagem_amigavel()
    assert "campo A inválido" in msg and "campo B inválido" in msg


# ---------------------------------------------------------------------------
# Contagem do progresso (v0.12.0)
# ---------------------------------------------------------------------------

def _lote_de_teste(tmp_path, monkeypatch):
    """Aponta o banco para uma pasta temporária.

    Cuidado: `Settings` lê os `os.getenv` na definição da classe, ou seja, uma
    única vez, quando o módulo é importado. Mexer em `os.environ` depois disso
    não muda nada — foi assim que estes testes escreveram no banco de
    desenvolvimento sem ninguém perceber. Por isso trocamos o atributo do
    objeto já construído, e não a variável de ambiente.
    """
    import app.config as cfg
    s = cfg.get_settings()
    monkeypatch.setattr(s, "database_url", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setattr(s, "data_dir", str(tmp_path))
    from app import storage
    storage.init_db()
    lote_id = storage.criar_lote("x.xls", 5, True, {})
    ids = []
    for i in range(5):
        ids.append(storage.registrar_item(
            lote_id, i, f"COD{i}0000", f"peca {i}", {},
            status="na_fila", valor=100.0 * (i + 1)))
    return storage, lote_id, ids


def test_contador_soma_qualquer_status_que_nao_seja_na_fila(tmp_path, monkeypatch):
    """O bug da v0.11.0: a tela somava uma lista fixa de status.

    'pronto_com_fotos' e 'foto_do_operador' não estavam na lista, então
    decidir uma peça não mexia no contador.
    """
    storage, lote_id, ids = _lote_de_teste(tmp_path, monkeypatch)
    assert storage.progresso_da_fila(lote_id)["_feitos"] == 0

    storage.atualizar_dados_catalogo(ids[0], status="pronto_com_fotos")
    storage.atualizar_dados_catalogo(ids[1], status="foto_do_operador")
    storage.atualizar_item(ids[2], status="publicado")

    p = storage.progresso_da_fila(lote_id)
    assert p["_feitos"] == 3
    assert p["_faltam"] == 2
    assert p["_total"] == 5


def test_decisao_fica_carimbada_com_a_hora(tmp_path, monkeypatch):
    storage, lote_id, ids = _lote_de_teste(tmp_path, monkeypatch)
    storage.atualizar_dados_catalogo(ids[0], status="pronto_com_fotos")
    assert storage.item(ids[0])["decidido_em"]
    assert storage.progresso_da_fila(lote_id)["_ultima_decisao"]


def test_carimbo_nao_e_reescrito_por_atualizacao_posterior(tmp_path, monkeypatch):
    """A hora guardada é a da PRIMEIRA decisão, não a do último toque."""
    storage, lote_id, ids = _lote_de_teste(tmp_path, monkeypatch)
    storage.atualizar_dados_catalogo(ids[0], status="pronto_com_fotos")
    primeira = storage.item(ids[0])["decidido_em"]
    storage.atualizar_item(ids[0], status="publicado")
    assert storage.item(ids[0])["decidido_em"] == primeira


def test_pular_nao_conta_como_decidida_mas_fica_registrado(tmp_path, monkeypatch):
    storage, lote_id, ids = _lote_de_teste(tmp_path, monkeypatch)
    storage.adiar_item(ids[4])
    p = storage.progresso_da_fila(lote_id)
    assert p["_feitos"] == 0
    assert p["_adiados"] == 1
    assert storage.item(ids[4])["adiado_vezes"] == 1
    storage.adiar_item(ids[4])
    assert storage.item(ids[4])["adiado_vezes"] == 2


def test_fila_retoma_de_onde_parou(tmp_path, monkeypatch):
    """Fechar e voltar tem que continuar na mesma peça."""
    storage, lote_id, ids = _lote_de_teste(tmp_path, monkeypatch)
    primeiro = storage.proximo_da_fila(lote_id)
    assert primeiro["id"] == ids[4]                     # maior valor
    storage.atualizar_dados_catalogo(ids[4], status="pronto_com_fotos")
    assert storage.proximo_da_fila(lote_id)["id"] == ids[3]


def test_diagnostico_de_persistencia_conta_decisoes(tmp_path, monkeypatch):
    storage, lote_id, ids = _lote_de_teste(tmp_path, monkeypatch)
    storage.atualizar_dados_catalogo(ids[0], status="pronto_com_fotos")
    d = storage.diagnostico_persistencia()
    assert d["existe"] is True
    assert d["decisoes_gravadas"] == 1
