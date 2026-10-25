"""Rotas de contatos: as funcoes puras que montam o que o cliente le."""
from types import SimpleNamespace as N

import contatos


def consulta(usuario, existe=True, negocio=False, servidor="s.whatsapp.net"):
    return N(IsIn=existe, JID=N(User=usuario if existe else "", Server=servidor),
             HasField=lambda campo: negocio and campo == "VerifiedName")


def test_alvo_de_contato_normaliza_as_formas_de_numero():
    assert contatos.alvo_de_contato("5511999990000") == "5511999990000@s.whatsapp.net"
    assert contatos.alvo_de_contato("5511999990000@c.us") == "5511999990000@s.whatsapp.net"
    assert contatos.alvo_de_contato("5511999990000:12@s.whatsapp.net") == \
        "5511999990000@s.whatsapp.net"


def test_alvo_de_contato_mantem_o_lid_inteiro():
    assert contatos.alvo_de_contato("123456789012@lid") == "123456789012@lid"


def test_alvo_de_contato_recusa_o_que_nao_e_pessoa():
    for texto in ("", None, "abc", "5511@c.us", "120363@g.us", "status@broadcast",
                  "5511999990000@newsletter"):
        assert contatos.alvo_de_contato(texto) == ""


def test_primeiro_destino_aceita_texto_ou_lista():
    assert contatos.primeiro_destino(["a", "b"]) == "a"
    assert contatos.primeiro_destino("a") == "a"
    assert contatos.primeiro_destino([]) == []


def test_formas_do_numero_tenta_o_nono_digito_dos_celulares_brasileiros():
    assert contatos.formas_do_numero("5511999990000") == ["5511999990000", "551199990000"]
    assert contatos.formas_do_numero("551199990000") == ["551199990000", "5511999990000"]


def test_formas_do_numero_nao_mexe_em_fixo_nem_em_outros_paises():
    assert contatos.formas_do_numero("551133334444") == ["551133334444"]
    assert contatos.formas_do_numero("14155550123") == ["14155550123"]


def test_achado_na_consulta_pega_o_primeiro_numero_que_o_whatsapp_conhece():
    achado = contatos.achado_na_consulta([consulta("", existe=False), consulta("5511888")])
    assert achado == {"jid": "5511888@s.whatsapp.net", "negocio": False}


def test_achado_na_consulta_marca_conta_comercial_e_aceita_vazio():
    assert contatos.achado_na_consulta([consulta("5511888", negocio=True)])["negocio"] is True
    assert contatos.achado_na_consulta([consulta("", existe=False)]) is None
    assert contatos.achado_na_consulta([]) is None
    assert contatos.achado_na_consulta(None) is None


def test_resposta_do_numero_que_existe_usa_o_id_canonico():
    achado = {"jid": "551199990000@s.whatsapp.net", "negocio": True}
    saida = contatos.resposta_do_numero("5511999990000", achado)
    assert saida["numberExists"] is True and saida["canReceiveMessage"] is True
    assert saida["status"] == 200 and saida["isBusiness"] is True
    assert saida["id"] == {"server": "c.us", "user": "551199990000",
                           "_serialized": "551199990000@c.us"}


def test_resposta_do_numero_que_nao_existe():
    saida = contatos.resposta_do_numero("5511999990000", None)
    assert saida["numberExists"] is False and saida["canReceiveMessage"] is False
    assert saida["status"] == 404
    assert saida["id"]["_serialized"] == "5511999990000@c.us"


def test_para_wid_so_aceita_numero_ou_lid():
    assert contatos.para_wid("+55 (11) 99999-0000") == "5511999990000@c.us"
    assert contatos.para_wid(["5511999990000"]) == "5511999990000@c.us"
    assert contatos.para_wid("5511999990000@s.whatsapp.net") == "5511999990000@c.us"
    assert contatos.para_wid("123456789012@lid") == "123456789012@lid"
    for ruim in ("", None, "123", "120363000000@g.us", "status@broadcast", "abc@lid"):
        assert contatos.para_wid(ruim) == ""


def test_validar_salvar_devolve_os_campos():
    campos, codigo = contatos.validar_salvar({
        "phone": "5511999990000", "name": " Ana ", "lastName": " Souza ",
        "syncAddressBook": True})
    assert codigo == ""
    assert campos == {"wid": "5511999990000@c.us", "primeiro": "Ana",
                      "sobrenome": "Souza", "sincronizar": True}


def test_validar_salvar_sincroniza_a_nao_ser_que_se_diga_que_nao():
    assert contatos.validar_salvar({"phone": "5511999990000", "name": "Ana"})[0]["sincronizar"]
    assert not contatos.validar_salvar(
        {"phone": "5511999990000", "name": "Ana", "syncAddressBook": False})[0]["sincronizar"]


def test_validar_salvar_confere_o_numero_antes_do_nome():
    assert contatos.validar_salvar({"phone": "12", "name": ""}) == (None, "contact_invalid")
    assert contatos.validar_salvar({"phone": "5511999990000", "name": "  "}) == \
        (None, "contact_name_required")
    assert contatos.validar_salvar(None) == (None, "contact_invalid")


def test_validar_salvar_corta_nomes_longos():
    campos, _ = contatos.validar_salvar({"phone": "5511999990000", "name": "a" * 300})
    assert len(campos["primeiro"]) == 100


def test_campos_da_agenda_leva_nome_completo_e_sincronia():
    campos = {"primeiro": "Ana", "sobrenome": "Souza", "sincronizar": True}
    assert contatos.campos_da_agenda(campos, "5511@s.whatsapp.net") == {
        "fullName": "Ana Souza", "firstName": "Ana", "saveOnPrimaryAddressbook": True,
        "pnJID": "5511@s.whatsapp.net"}
    campos = {"primeiro": "Ana", "sobrenome": "", "sincronizar": False}
    saida = contatos.campos_da_agenda(campos, "5511@s.whatsapp.net", "99@lid")
    assert saida["fullName"] == "Ana" and saida["lidJID"] == "99@lid"
    assert saida["saveOnPrimaryAddressbook"] is False


def test_erro_de_contato_usa_o_codigo_e_o_status_do_servidor_de_referencia():
    assert contatos.erro_de_contato("contact_invalid") == (
        400, {"status": "error", "code": "contact_invalid"})
    assert contatos.erro_de_contato("contact_lid_without_phone")[0] == 400
    assert contatos.erro_de_contato("contact_not_available")[0] == 503
    assert contatos.erro_de_contato("contact_operation_failed")[0] == 502


def test_erro_de_contato_nunca_repete_texto_de_excecao():
    assert contatos.erro_de_contato("Falha com 5511999990000") == (
        502, {"status": "error", "code": "contact_operation_failed"})


def test_resposta_de_salvar_devolve_o_id_em_que_foi_salvo():
    assert contatos.resposta_de_salvar("551199990000:5@s.whatsapp.net", True) == {
        "id": "551199990000@c.us", "isMyContact": True, "syncToAddressbook": True}
    assert contatos.resposta_de_salvar("5511@s.whatsapp.net", False)["syncToAddressbook"] is False


def test_vcard_leva_o_nome_e_o_waid():
    cartao = contatos.vcard_de("Ana Souza", "5511999990000")
    assert cartao.startswith("BEGIN:VCARD\nVERSION:3.0\n")
    assert "FN:Ana Souza\n" in cartao
    assert "TEL;type=CELL;waid=5511999990000:+5511999990000\n" in cartao
    assert cartao.endswith("END:VCARD")


def test_vcard_escapa_o_que_quebraria_o_cartao():
    cartao = contatos.vcard_de("Ana;\nSouza, Jr", "5511999990000")
    assert "FN:Ana; Souza, Jr" not in cartao
    assert "FN:Ana\\; Souza\\, Jr\n" in cartao


def test_nome_do_cartao_prefere_pedido_depois_agenda_depois_apelido_depois_numero():
    assert contatos.nome_do_cartao({"name": "Ana"}, "55", "Mae") == "Mae"
    assert contatos.nome_do_cartao({"name": "Ana", "pushname": "A"}, "55") == "Ana"
    assert contatos.nome_do_cartao({"pushname": "A"}, "55") == "A"
    assert contatos.nome_do_cartao({}, "55", "  ") == "55"


def test_nome_do_conjunto_junta_os_nomes():
    assert contatos.nome_do_conjunto(["Ana", "Bia"]) == "Ana, Bia"


class RoteadorFalso:
    def __init__(self):
        self.rotas = []

    def add_get(self, caminho, _):
        self.rotas.append(("GET", caminho))

    def add_post(self, caminho, _):
        self.rotas.append(("POST", caminho))


def test_registrar_liga_as_rotas_de_contato():
    r = RoteadorFalso()
    contatos.registrar(r, sessao=None, ok=None, jid_de_texto=None, jid_texto=None,
                       destino_de=None, guardar_enviada=None, contexto_citado=None)
    assert set(r.rotas) == {
        ("POST", "/api/{tok}/block-contact"),
        ("POST", "/api/{tok}/unblock-contact"),
        ("GET", "/api/{tok}/check-number-status/{phone}"),
        ("POST", "/api/{tok}/save-contact"),
        ("POST", "/api/{tok}/contact-vcard"),
    }


def test_jids_equivalentes_poe_a_forma_pedida_por_ultimo():
    assert contatos.jids_equivalentes("5511999990000@s.whatsapp.net") == [
        "551199990000@s.whatsapp.net", "5511999990000@s.whatsapp.net"]
    assert contatos.jids_equivalentes("551199990000@s.whatsapp.net") == [
        "5511999990000@s.whatsapp.net", "551199990000@s.whatsapp.net"]
    assert contatos.jids_equivalentes("") == []


def test_jids_equivalentes_deixa_lid_e_outros_paises_como_estao():
    assert contatos.jids_equivalentes("5511999990000@lid") == ["5511999990000@lid"]
    assert contatos.jids_equivalentes("14155550123@s.whatsapp.net") == [
        "14155550123@s.whatsapp.net"]
    assert contatos.jids_equivalentes("1203-44@g.us") == ["1203-44@g.us"]


def test_entrada_de_bloqueio_usa_o_numero_quando_o_bloqueio_esta_no_lid():
    assert contatos.entrada_de_bloqueio("123456789012345@lid", "5511999990000@s.whatsapp.net") == {
        "phone": "5511999990000", "id": {"_serialized": "123456789012345@lid"}}
    assert contatos.entrada_de_bloqueio("5511999990000:12@s.whatsapp.net") == {
        "phone": "5511999990000", "id": {"_serialized": "5511999990000@c.us"}}
    # sem o mapa, ou com um "numero" que e outro LID, ficam os digitos do proprio LID
    assert contatos.entrada_de_bloqueio("123456789012345@lid")["phone"] == "123456789012345"
    assert contatos.entrada_de_bloqueio("123456789012345@lid", "999@lid")["phone"] == "123456789012345"


def test_resposta_pn_lid_traz_os_campos_dos_dois_leitores_do_cliente():
    r = contatos.resposta_pn_lid("5511999990000@s.whatsapp.net", "123@lid",
                                 {"name": "Ana", "pushname": ""})
    numero = {"_serialized": "5511999990000@c.us"}
    assert r == {"lid": {"_serialized": "123@lid"}, "phone": numero, "id": numero,
                 "phoneNumber": numero, "contact": {"name": "Ana", "pushname": None}}


def test_corpo_do_contato_marca_conta_comercial_so_com_nome_verificado():
    comercial = contatos.corpo_do_contato("5511@s.whatsapp.net", "5511@s.whatsapp.net",
                                          {"pushname": "Loja"}, "Loja Oficial")
    assert comercial["isBusiness"] is True and comercial["verifiedName"] == "Loja Oficial"
    assert comercial["phoneNumber"] == {"_serialized": "5511@c.us"}
    comum = contatos.corpo_do_contato("9@lid", "", {"name": "Ana"})
    assert comum["isBusiness"] is False and "verifiedName" not in comum
    assert comum["id"] == {"_serialized": "9@lid"} and "phoneNumber" not in comum
    assert comum["isMyContact"] is True and comum["name"] == "Ana"


def test_registro_da_agenda_so_sincroniza_quem_tem_nome_e_acha_a_propria_conta():
    meus = {"5511@c.us", "9@lid"}
    com_nome = contatos.registro_da_agenda(
        {"name": "Ana", "pushname": ""}, {"_serialized": "5522@c.us"}, meus)
    assert com_nome["syncToAddressbook"] is True and com_nome["isMyContact"] is True
    assert com_nome["isMe"] is False and com_nome["pushname"] is None
    sem_nome = contatos.registro_da_agenda(
        {"name": "", "pushname": "ap"}, {"_serialized": "9@lid"}, meus)
    assert sem_nome["syncToAddressbook"] is False and sem_nome["isMe"] is True


def test_alvo_de_assinatura_aceita_pessoa_e_grupo_e_nada_mais():
    assert contatos.alvo_de_assinatura("5511999990000@c.us") == "5511999990000@s.whatsapp.net"
    assert contatos.alvo_de_assinatura("123456789012@lid") == "123456789012@lid"
    assert contatos.alvo_de_assinatura("1203-44@g.us") == "1203-44@g.us"
    for ruim in ("", None, "status@broadcast", "abc", "5511999990000@newsletter"):
        assert contatos.alvo_de_assinatura(ruim) == ""
