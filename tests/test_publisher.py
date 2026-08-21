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


# ---------------------------------------------------------------------------
# Tela de prontas e publicação em lote (v0.13.0)
# ---------------------------------------------------------------------------

def test_descartadas_pela_triagem_nao_contam_como_decididas(tmp_path, monkeypatch):
    """Relato real: '353 de 1687 decididas' logo depois de montar a fila.

    Os 353 eram itens que a triagem descartou por não ter código utilizável
    ('sem_dado'). Ninguém trabalhou neles — não podem entrar na barra.
    """
    storage, lote_id, ids = _lote_de_teste(tmp_path, monkeypatch)
    storage.atualizar_dados_catalogo(ids[0], status="sem_dado")
    storage.atualizar_dados_catalogo(ids[1], status="sem_dado")

    p = storage.progresso_da_fila(lote_id)
    assert p["_fora_da_fila"] == 2
    assert p["_total"] == 3            # 5 na planilha, 3 trabalháveis
    assert p["_feitos"] == 0
    assert p["_faltam"] == 3


def test_lista_de_prontas_traz_os_tres_caminhos(tmp_path, monkeypatch):
    storage, lote_id, ids = _lote_de_teste(tmp_path, monkeypatch)
    storage.atualizar_dados_catalogo(ids[0], status="pronto_com_fotos")
    storage.atualizar_dados_catalogo(ids[1], status="foto_do_operador")
    storage.atualizar_dados_catalogo(ids[2], status="aguardando_aprovacao")
    storage.atualizar_dados_catalogo(ids[3], status="nao_encontrado")

    prontas = storage.itens_prontos_para_publicar(lote_id)
    assert {p["id"] for p in prontas} == {ids[0], ids[1], ids[2]}
    assert storage.progresso_da_fila(lote_id)["_prontos"] == 3


def test_prontas_vem_ordenada_por_valor(tmp_path, monkeypatch):
    """A peça mais cara parada primeiro — é onde está o dinheiro."""
    storage, lote_id, ids = _lote_de_teste(tmp_path, monkeypatch)
    for i in ids[:3]:
        storage.atualizar_dados_catalogo(i, status="pronto_com_fotos")
    prontas = storage.itens_prontos_para_publicar(lote_id)
    valores = [p["valor"] for p in prontas]
    assert valores == sorted(valores, reverse=True)


def test_publicar_sem_selecionar_nada_nao_chama_o_ml(tmp_path, monkeypatch):
    import asyncio
    from app.publisher import publicar_selecionados
    r = asyncio.run(publicar_selecionados([]))
    assert r["ok"] is False
    assert r["resultados"] == []


def test_rota_de_publicar_exige_a_palavra_publicar(tmp_path, monkeypatch):
    """Sem digitar PUBLICAR, nada sai — a conta é real."""
    from fastapi.testclient import TestClient
    from app.main import app
    storage, lote_id, ids = _lote_de_teste(tmp_path, monkeypatch)
    storage.atualizar_dados_catalogo(ids[0], status="pronto_com_fotos")

    cli = TestClient(app, base_url="https://testserver")
    r = cli.post(f"/lotes/{lote_id}/publicar-selecionadas",
                 data={"ids": [ids[0]], "confirmacao": "sim"})
    assert r.status_code == 400
    assert storage.item(ids[0])["status"] == "pronto_com_fotos"


def test_peca_que_nao_esta_pronta_e_recusada_sem_ir_ao_ml(tmp_path, monkeypatch):
    import asyncio
    from app import publisher
    storage, lote_id, ids = _lote_de_teste(tmp_path, monkeypatch)
    storage.atualizar_dados_catalogo(ids[0], status="nao_encontrado")

    class ContaOk:
        async def diagnostico_conta(self):
            return {"apta": True, "impedimentos": []}

    monkeypatch.setattr(publisher, "MLClient", lambda *a, **k: ContaOk())
    r = asyncio.run(publisher.publicar_selecionados([ids[0]]))
    assert r["publicados"] == 0
    assert "não está pronta" in r["resultados"][0]["mensagem"]


# ---------------------------------------------------------------------------
# Modelo User Product do Mercado Livre (v0.14.0)
# ---------------------------------------------------------------------------

def test_anuncio_proprio_leva_family_name():
    """Erro real na primeira publicação de verdade (injetor A2C59517051):

    'The body does not contains some or none of the following properties
     [family_name]'
    """
    p = montar_payload(item(), 3188.83, category_id="MLB1747")
    assert p["family_name"]
    assert len(p["family_name"]) <= 60


def test_family_name_e_campo_de_topo_nao_atributo():
    p = montar_payload(item(), 100.0, category_id="MLB1747")
    ids = {a["id"] for a in p["attributes"]}
    assert "FAMILY_NAME" not in ids
    assert "family_name" in p


def test_anuncio_proprio_declara_garantia():
    p = montar_payload(item(), 100.0, category_id="MLB1747")
    termos = {t["id"]: t["value_name"] for t in p["sale_terms"]}
    assert termos["WARRANTY_TYPE"]
    assert termos["WARRANTY_TIME"]


def test_catalogo_nao_leva_family_name_nem_garantia():
    """No catálogo a identidade do produto vem de lá; mandar os nossos briga."""
    p = montar_payload(item(), 100.0, catalog_product_id="MLB123",
                       category_id="MLB1747")
    assert "family_name" not in p
    assert "sale_terms" not in p


def test_family_name_respeita_o_limite_com_titulo_longo():
    from app.publisher import aplicar_user_product, FAMILY_NAME_MAX
    payload = aplicar_user_product({}, "X" * 200)
    assert len(payload["family_name"]) == FAMILY_NAME_MAX


def test_garantia_ja_informada_nao_e_duplicada():
    from app.publisher import aplicar_user_product
    payload = {"sale_terms": [{"id": "WARRANTY_TYPE",
                               "value_name": "Garantia de fábrica"}]}
    aplicar_user_product(payload, "Peça")
    tipos = [t for t in payload["sale_terms"] if t["id"] == "WARRANTY_TYPE"]
    assert len(tipos) == 1
    assert tipos[0]["value_name"] == "Garantia de fábrica"


def test_erro_de_family_name_vira_mensagem_explicativa():
    erro = MLApiError(400, {"message": "The body does not contains some or "
                            "none of the following properties [family_name]"},
                      "/items")
    assert "family_name" in erro.mensagem_amigavel()
