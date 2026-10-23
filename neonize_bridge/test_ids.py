import server


def test_partes_do_id_privado_e_grupo():
    s = server.Sessao
    assert s.partes_do_id("true_5551@c.us_ABC") == (True, "5551@s.whatsapp.net", "ABC", "")
    assert s.partes_do_id("false_123@g.us_XYZ_9@lid") == (False, "123@g.us", "XYZ", "9@lid")
    assert s.partes_do_id("solto") == (False, "", "solto", "")
