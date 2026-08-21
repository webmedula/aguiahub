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


def test_anuncio_proprio_leva_family_name_e_atributos():
    """No modelo User Product o nome vai em family_name, não em title.

    Erro real do ML na segunda camada da validação:
    'The fields [title] are invalid for requested call.'
    """
    p = montar_payload(item(), 100.0, category_id="MLB1747")
    assert p["family_name"]
    assert "title" not in p
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


def test_publicar_sem_conta_conectada_nao_muda_nada(tmp_path, monkeypatch):
    """Sem conta do ML conectada, a rota recusa e o item fica intacto.

    Este teste substituiu o que exigia a palavra PUBLICAR digitada (removida
    na v0.17.0 a pedido do João). Registro aqui porque o teste antigo passou a
    passar pelo motivo errado: ele batia no 400 de "conecte a conta", não no
    da confirmação, e teria continuado verde mesmo com a trava toda aberta.
    """
    from fastapi.testclient import TestClient
    from app.main import app
    storage, lote_id, ids = _lote_de_teste(tmp_path, monkeypatch)
    storage.atualizar_dados_catalogo(ids[0], status="pronto_com_fotos")
    storage.desconectar()

    cli = TestClient(app, base_url="https://testserver")
    r = cli.post(f"/lotes/{lote_id}/publicar-selecionadas",
                 data={"ids": [ids[0]]})
    assert r.status_code == 400
    assert "Mercado Livre" in r.text
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


# ---------------------------------------------------------------------------
# Diagnóstico de erro do ML (v0.15.0)
# ---------------------------------------------------------------------------

def test_body_invalid_fields_sozinho_traz_a_resposta_completa():
    """Relato real: a tela mostrou só 'body.invalid_fields'.

    Esse texto é rótulo de validação, não explicação — não dá para agir sobre
    ele. Quando o ML não detalha a causa, devolvemos o corpo inteiro.
    """
    erro = MLApiError(400, {"message": "body.invalid_fields",
                            "error": "validation_error", "cause": []}, "/items")
    msg = erro.mensagem_amigavel()
    assert "body.invalid_fields" in msg
    assert "validation_error" in msg          # veio o corpo junto


def test_causa_sem_message_usa_o_code_e_as_referencias():
    """As causas do ML nem sempre têm 'message' — ler só ela dava lista vazia."""
    erro = MLApiError(400, {"message": "body.invalid_fields",
                            "cause": [{"code": "item.attributes.missing",
                                       "references": ["BRAND"]}]}, "/items")
    msg = erro.mensagem_amigavel()
    assert "item.attributes.missing" in msg
    assert "BRAND" in msg


def test_detalhe_tecnico_e_json_valido():
    import json as _json
    corpo = {"message": "x", "cause": [{"code": "y"}]}
    erro = MLApiError(400, corpo, "/items")
    assert _json.loads(erro.detalhe_tecnico()) == corpo


def test_detalhe_tecnico_aguenta_corpo_nao_serializavel():
    erro = MLApiError(500, object(), "/items")
    assert erro.detalhe_tecnico()             # não levanta


def test_categoria_com_subcategorias_e_recusada():
    """O ML só publica em categoria folha; a de meio de árvore volta como
    'body.invalid_fields', sem dizer qual campo."""
    import asyncio
    from app.ml.catalog import categoria_e_folha

    class Cli:
        async def get(self, caminho, **kw):
            return {"id": "MLB1747", "children_categories": [{"id": "MLB1748"}],
                    "path_from_root": [{"name": "Acessórios para Veículos"}]}

    folha, motivo = asyncio.run(categoria_e_folha(Cli(), "MLB1747"))
    assert folha is False
    assert "categoria final" in motivo


def test_categoria_folha_e_aceita():
    import asyncio
    from app.ml.catalog import categoria_e_folha

    class Cli:
        async def get(self, caminho, **kw):
            return {"id": "MLB123456", "children_categories": []}

    folha, motivo = asyncio.run(categoria_e_folha(Cli(), "MLB123456"))
    assert folha is True and motivo == ""


def test_teste_nao_publica_nada(tmp_path, monkeypatch):
    """A tela de teste não pode criar anúncio — é o ponto dela."""
    import asyncio
    from app import publisher
    storage, lote_id, ids = _lote_de_teste(tmp_path, monkeypatch)
    storage.atualizar_dados_catalogo(ids[0], status="pronto_com_fotos",
                                     catalog_category_id="MLB999",
                                     loja_nome="PEÇA DE TESTE",
                                     loja_fotos='["https://x/a.jpg"]')
    chamadas = []

    class Cli:
        async def get(self, caminho, **kw):
            chamadas.append(("GET", caminho))
            return {"children_categories": []}

        async def post(self, caminho, json, **kw):
            chamadas.append(("POST", caminho))
            raise AssertionError("o teste não pode chamar POST /items")

        async def validar_item(self, payload):
            chamadas.append(("VALIDATE", "/items/validate"))
            return {"disponivel": True, "ok": True, "status": 204,
                    "problemas": [], "corpo": None}

    monkeypatch.setattr(publisher, "MLClient", lambda *a, **k: Cli())
    r = asyncio.run(publisher.testar_item(ids[0]))
    assert r["validacao"]["ok"] is True
    assert ("POST", "/items") not in chamadas
    assert storage.item(ids[0])["status"] == "pronto_com_fotos"


# ---------------------------------------------------------------------------
# Histórico e revisão de decisões (v0.16.0)
# ---------------------------------------------------------------------------

def test_historico_lista_o_que_foi_decidido(tmp_path, monkeypatch):
    storage, lote_id, ids = _lote_de_teste(tmp_path, monkeypatch)
    storage.atualizar_dados_catalogo(ids[0], status="nao_encontrado")
    storage.atualizar_dados_catalogo(ids[1], status="pronto_com_fotos")

    decididas = storage.itens_decididos(lote_id)
    assert {d["id"] for d in decididas} == {ids[0], ids[1]}


def test_historico_nao_traz_quem_esta_na_fila_nem_fora_dela(tmp_path, monkeypatch):
    storage, lote_id, ids = _lote_de_teste(tmp_path, monkeypatch)
    storage.atualizar_dados_catalogo(ids[0], status="sem_dado")
    storage.atualizar_dados_catalogo(ids[1], status="nao_selecionado")
    assert storage.itens_decididos(lote_id) == []


def test_historico_filtra_por_situacao_e_por_busca(tmp_path, monkeypatch):
    storage, lote_id, ids = _lote_de_teste(tmp_path, monkeypatch)
    storage.atualizar_dados_catalogo(ids[0], status="erro")
    storage.atualizar_dados_catalogo(ids[1], status="nao_encontrado")

    assert len(storage.itens_decididos(lote_id, status="erro")) == 1
    achados = storage.itens_decididos(lote_id, busca="cod0")
    assert achados and achados[0]["id"] == ids[0]


def test_rever_devolve_a_peca_para_a_fila(tmp_path, monkeypatch):
    storage, lote_id, ids = _lote_de_teste(tmp_path, monkeypatch)
    storage.atualizar_dados_catalogo(ids[0], status="nao_encontrado",
                                     erro="operador não achou")
    assert storage.reabrir_item(ids[0]) is True

    item = storage.item(ids[0])
    assert item["status"] == "na_fila"
    assert item["decidido_em"] is None
    assert item["erro"] is None


def test_rever_preserva_o_que_ja_tinha_sido_descoberto(tmp_path, monkeypatch):
    """Refazer a busca à toa seria castigo, não correção."""
    storage, lote_id, ids = _lote_de_teste(tmp_path, monkeypatch)
    storage.atualizar_dados_catalogo(ids[0], status="pronto_com_fotos",
                                     loja_nome="PEÇA X",
                                     loja_fotos='["https://x/a.jpg"]')
    storage.reabrir_item(ids[0])
    item = storage.item(ids[0])
    assert item["loja_nome"] == "PEÇA X"
    assert item["loja_fotos"] == '["https://x/a.jpg"]'


def test_peca_publicada_nao_pode_ser_revertida(tmp_path, monkeypatch):
    """Desfazer aqui não apaga o anúncio no ML — a tela mentiria."""
    storage, lote_id, ids = _lote_de_teste(tmp_path, monkeypatch)
    storage.atualizar_item(ids[0], status="publicado", ml_item_id="MLB1")
    assert storage.reabrir_item(ids[0]) is False
    assert storage.item(ids[0])["status"] == "publicado"


def test_peca_pulada_volta_com_valor_positivo(tmp_path, monkeypatch):
    """'Pular' negativa o valor para ir ao fim da fila; rever tem que desfazer."""
    storage, lote_id, ids = _lote_de_teste(tmp_path, monkeypatch)
    storage.adiar_item(ids[0])
    assert storage.item(ids[0])["valor"] < 0
    storage.reabrir_item(ids[0])
    assert storage.item(ids[0])["valor"] > 0


def test_nao_selecionadas_podem_ser_trazidas_para_a_fila(tmp_path, monkeypatch):
    storage, lote_id, ids = _lote_de_teste(tmp_path, monkeypatch)
    storage.atualizar_dados_catalogo(ids[0], status="nao_selecionado")
    assert [f["id"] for f in storage.itens_fora_da_fila(lote_id)] == [ids[0]]
    assert storage.reabrir_item(ids[0]) is True
    assert storage.item(ids[0])["status"] == "na_fila"


def test_title_e_removido_quando_vai_family_name():
    """Regressão da cascata de validação do ML.

    Rodada 1: 'does not contains ... [family_name]'  -> passei a mandar.
    Rodada 2: 'The fields [title] are invalid'       -> tem que parar de mandar.
    """
    from app.publisher import aplicar_user_product
    payload = {"title": "Bico Injetor Ranger", "attributes": []}
    aplicar_user_product(payload, "Bico Injetor Ranger")
    assert "title" not in payload
    assert payload["family_name"] == "Bico Injetor Ranger"


def test_family_name_aproveita_o_title_quando_nao_vem_titulo():
    from app.publisher import aplicar_user_product
    payload = {"title": "Módulo de Injeção Fiat Toro"}
    aplicar_user_product(payload, "")
    assert payload["family_name"] == "Módulo de Injeção Fiat Toro"


def test_catalogo_continua_com_title_ausente_e_sem_family_name():
    p = montar_payload(item(), 100.0, catalog_product_id="MLB123",
                       category_id="MLB1747")
    assert "title" not in p and "family_name" not in p


def test_publicar_com_conta_conectada_nao_pede_mais_confirmacao(tmp_path,
                                                                monkeypatch):
    """v0.17.0: o botão publica direto, sem campo de texto."""
    from datetime import datetime, timezone, timedelta
    from fastapi.testclient import TestClient
    from app.ml.oauth import TokenBundle
    import app.main as main

    storage, lote_id, ids = _lote_de_teste(tmp_path, monkeypatch)
    storage.atualizar_dados_catalogo(ids[0], status="pronto_com_fotos")
    storage.salvar_token(TokenBundle(
        access_token="t", refresh_token="r", user_id=1,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=5),
        scope=""), "aguia")

    chamou = {}

    async def falso_publicar(ids_, *a, **kw):
        chamou["ids"] = ids_
        return {"ok": True, "publicados": 1, "falharam": 0,
                "mensagem": "1 anúncio(s) publicado(s).", "resultados": []}

    monkeypatch.setattr(main, "publicar_selecionados", falso_publicar)
    cli = TestClient(app=main.app, base_url="https://testserver")
    r = cli.post(f"/lotes/{lote_id}/publicar-selecionadas", data={"ids": [ids[0]]})

    assert r.status_code == 200
    assert chamou["ids"] == [ids[0]]


def test_nenhuma_tela_pede_a_palavra_publicar():
    """Garante que não sobrou campo de confirmação em template nenhum."""
    from pathlib import Path
    pasta = Path(__file__).resolve().parent.parent / "app" / "templates"
    for arquivo in pasta.glob("*.html"):
        texto = arquivo.read_text(encoding="utf-8")
        assert 'name="confirmacao"' not in texto, arquivo.name
