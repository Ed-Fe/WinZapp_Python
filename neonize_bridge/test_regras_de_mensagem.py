import regras_de_mensagem as regras

AGORA = 1_700_000_000


def test_edicao_recusada_quando_a_mensagem_nao_e_minha():
    assert regras.motivo_da_recusa(False, {"t": AGORA}, AGORA) == "not_mine"


def test_edicao_recusada_quando_a_ponte_nao_guardou_a_mensagem():
    assert regras.motivo_da_recusa(True, None, AGORA) == "not_stored"


def test_edicao_recusada_fora_da_janela_de_15_minutos():
    assert regras.motivo_da_recusa(True, {"t": AGORA - 16 * 60}, AGORA) == "outside_window"
    assert regras.motivo_da_recusa(True, {"t": AGORA - 14 * 60}, AGORA) == ""


def test_edicao_sem_hora_guardada_nao_e_recusada_por_janela():
    assert regras.motivo_da_recusa(True, {}, AGORA) == ""


def test_apagar_para_todos_mensagem_minha_e_assinada_por_mim():
    assert regras.autor_da_revogacao(True, False, "eu@s.whatsapp.net", "", {}) == "eu@s.whatsapp.net"


def test_apagar_para_todos_mensagem_de_outro_so_em_grupo():
    assert regras.autor_da_revogacao(False, False, "eu@s.whatsapp.net", "", {}) == ""
    assert regras.autor_da_revogacao(
        False, True, "eu@s.whatsapp.net", "5551@s.whatsapp.net", {}) == "5551@s.whatsapp.net"


def test_apagar_para_todos_em_grupo_usa_o_autor_guardado_se_o_id_nao_traz():
    guardada = {"author": "5551@c.us"}
    assert regras.autor_da_revogacao(
        False, True, "eu@s.whatsapp.net", "", guardada) == "5551@s.whatsapp.net"
    assert regras.autor_da_revogacao(False, True, "eu@s.whatsapp.net", "", {}) == ""
