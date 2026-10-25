"""Rotas de etiquetas: as funcoes puras que montam o que a ponte grava e responde."""
import etiquetas


def test_primeira_etiqueta_da_ponte_nao_colide_com_as_do_celular():
    assert etiquetas.proximo_id({}) == "1000"


def test_proximo_id_segue_o_maior_ja_usado():
    assert etiquetas.proximo_id({"1000": {}, "1004": {}, "3": {}}) == "1005"


def test_cor_pedida_valida_e_respeitada_e_o_resto_reveza():
    assert etiquetas.indice_de_cor({"labelColor": 7}, {}) == 7
    assert etiquetas.indice_de_cor({"color": 0}, {"a": {}}) == 0
    assert etiquetas.indice_de_cor(None, {"a": {}, "b": {}}) == 2
    assert etiquetas.indice_de_cor({"labelColor": 4278190335}, {}) == 0
    assert etiquetas.indice_de_cor({"labelColor": True}, {}) == 0
    assert etiquetas.indice_de_cor(None, {str(i): {} for i in range(21)}) == 1


def test_criar_registra_e_devolve_a_entrada_do_wpp():
    registro = {}
    nova = etiquetas.criar(registro, "Clientes", {"labelColor": 5})
    assert nova == {"id": "1000", "name": "Clientes", "colorIndex": 5, "count": 0}
    assert registro["1000"]["chats"] == []
    assert etiquetas.criar(registro, "Fornecedores", None)["id"] == "1001"


def test_lista_sai_em_ordem_numerica_com_a_contagem_de_conversas():
    registro = {"1010": {"name": "B", "colorIndex": 1, "chats": ["a@g.us", "b@g.us"]},
                "1002": {"name": "A", "colorIndex": 0, "chats": []}}
    assert etiquetas.lista_para_wpp(registro) == [
        {"id": "1002", "name": "A", "colorIndex": 0, "count": 0},
        {"id": "1010", "name": "B", "colorIndex": 1, "count": 2}]


def test_aplicar_marca_desmarca_e_ignora_etiqueta_desconhecida():
    registro = {"1000": {"name": "X", "colorIndex": 0, "chats": []}}
    etiquetas.aplicar(registro, "1000", "5511@s.whatsapp.net", True)
    etiquetas.aplicar(registro, "1000", "5511@s.whatsapp.net", True)
    assert registro["1000"]["chats"] == ["5511@s.whatsapp.net"]
    etiquetas.aplicar(registro, "1000", "5511@s.whatsapp.net", False)
    assert registro["1000"]["chats"] == []
    etiquetas.aplicar(registro, "7", "5511@s.whatsapp.net", True)
    assert "7" not in registro


def test_pedidos_de_etiqueta_le_o_formato_do_wpp_e_ignora_o_resto():
    opcoes = [{"labelId": 76, "type": "add"}, {"labelId": "3", "type": "REMOVE"},
              {"labelId": "4"}, {"type": "add"}, {"labelId": "5", "type": "x"}, "lixo"]
    assert etiquetas.pedidos_de_etiqueta(opcoes) == [
        ("76", True), ("3", False), ("4", True)]
    assert etiquetas.pedidos_de_etiqueta(None) == []


def test_mutacao_de_edicao_tem_o_indice_do_whatsmeow():
    m = etiquetas.mutacao_edicao(1000, "Clientes", 3, False, 99)
    assert m["indice"] == ["label_edit", "1000"]
    assert m["valor"] == {"timestamp": 99, "labelEditAction": {
        "name": "Clientes", "color": 3, "deleted": False}}


def test_mutacao_de_conversa_leva_o_jid_no_indice():
    m = etiquetas.mutacao_conversa("1000", "5511@s.whatsapp.net", False, 7)
    assert m["indice"] == ["label_jid", "1000", "5511@s.whatsapp.net"]
    assert m["valor"]["labelAssociationAction"] == {"labeled": False}


def test_guardar_e_carregar_ida_e_volta(tmp_path):
    assert etiquetas.carregar(tmp_path) == {}
    registro = {"1000": {"name": "Cliente ç", "colorIndex": 2, "chats": ["a@g.us"]}}
    etiquetas.guardar(tmp_path, registro)
    assert etiquetas.carregar(tmp_path) == registro


def test_arquivo_corrompido_vira_registro_vazio(tmp_path):
    (tmp_path / etiquetas.ARQUIVO).write_text("{nao e json", encoding="utf-8")
    assert etiquetas.carregar(tmp_path) == {}
