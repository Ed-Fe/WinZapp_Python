from store import Armazem


def _msg(i, ts):
    return {"id": {"id": i}, "t": ts, "body": i}


def test_ultimas_e_anteriores(tmp_path):
    a = Armazem(str(tmp_path / "x.db"))
    a.mensagens_em_lote([("c", _msg(f"m{n}", 100 + n)) for n in range(10)])
    assert [m["body"] for m in a.mensagens("c", 3)] == ["m7", "m8", "m9"]
    assert [m["body"] for m in a.mensagens("c", 3, "m7")] == ["m4", "m5", "m6"]
    assert a.ultima("c")["body"] == "m9"
    assert a.mensagens("outra", 5) == []


def test_conversa_conta_nao_lidas_e_ordena(tmp_path):
    a = Armazem(str(tmp_path / "x.db"))
    a.conversa("a", name="A", ts=5)
    a.ao_chegar("a", 9, False)
    a.ao_chegar("a", 7, True)
    a.ao_chegar("b", 20, False)
    lista = a.conversas()
    assert [c["jid"] for c in lista] == ["b", "a"]
    assert lista[1]["unread"] == 1 and lista[1]["ts"] == 9 and lista[1]["name"] == "A"


def test_ficha_da_midia_por_id(tmp_path):
    a = Armazem(str(tmp_path / "x.db"))
    a.mensagem("c", _msg("m1", 1), b"ficha")
    a.mensagem("c", _msg("m2", 2))
    assert a.ficha("m1") == b"ficha"
    assert a.ficha("m2") is None and a.ficha("nao-existe") is None


def test_contato_de_mescla_formas_do_jid(tmp_path):
    a = Armazem(str(tmp_path / "x.db"))
    a.contato("1@lid", pushname="Ana")
    a.contato("5551@s.whatsapp.net", name="Ana Silva")
    assert a.contato_de("1@lid", "5551@s.whatsapp.net") == {"pushname": "Ana", "name": "Ana Silva"}
    assert a.contato_de("x@lid", "") == {}


def test_recebidas_a_ler_so_as_ultimas_nao_lidas(tmp_path):
    a = Armazem(str(tmp_path / "x.db"))
    for n in range(10):
        w = _msg(f"m{n}", 100 + n)
        w.update(fromMe=(n == 9), **{"from": "x@s.whatsapp.net"})
        a.mensagem("c", w)
    a.conversa("c", unread=3)
    achadas = a.recebidas_a_ler("c")
    assert [i for i, _, _ in achadas] == ["m6", "m7", "m8"]     # m9 e minha
    assert a.conversas()[0]["unread"] == 0
    assert a.recebidas_a_ler("c") == []                          # ja lidas


def test_ultima_recebida_ignora_as_minhas(tmp_path):
    a = Armazem(str(tmp_path / "x.db"))
    for n, minha in ((1, False), (2, False), (3, True)):
        w = _msg(f"m{n}", 100 + n)
        w["fromMe"] = minha
        a.mensagem("c", w)
    assert a.ultima_recebida("c") == "m2"
    assert a.ultima_recebida("vazia") == ""


def test_aplicar_revogacoes_poe_a_original_como_apagada(tmp_path):
    a = Armazem(str(tmp_path / "x.db"))
    a.mensagem("c", {"id": {"id": "ORIG"}, "t": 1, "type": "chat", "body": "oi"})
    a.mensagem("c", {"id": {"id": "AVISO"}, "t": 2, "type": "revoked", "body": "",
                     "protocolMessageKey": {"id": "ORIG"}})
    assert a.aplicar_revogacoes() == 1
    original = a.mensagem_wpp("ORIG")
    assert original["type"] == "revoked" and original["body"] == ""


def test_reacoes_trocam_e_somem(tmp_path):
    a = Armazem(str(tmp_path / "x.db"))
    a.reagir("m1", "eu", "A", 1)
    a.reagir("m1", "5551@c.us", "B", 2)
    a.reagir("m1", "eu", "C", 3)                 # trocou a propria reacao
    assert dict(a.reacoes_de("m1")) == {"eu": "C", "5551@c.us": "B"}
    a.reagir("m1", "eu", "", 4)                  # tirou
    assert a.reacoes_de("m1") == [("5551@c.us", "B")]
    assert a.reacoes_de("outra") == []


def test_votos_trocam_e_somem(tmp_path):
    a = Armazem(str(tmp_path / "x.db"))
    a.votar("p1", "eu", [0], 1)
    a.votar("p1", "5551@c.us", [1, 2], 2)
    a.votar("p1", "eu", [2], 3)                  # mudou o voto
    assert dict(a.votos_de("p1")) == {"eu": [2], "5551@c.us": [1, 2]}
    a.votar("p1", "eu", [], 4)                   # tirou o voto
    assert a.votos_de("p1") == [("5551@c.us", [1, 2])]


def test_mensagens_fixadas_respeitam_o_prazo(tmp_path):
    a = Armazem(str(tmp_path / "x.db"))
    a.mensagens_em_lote([("c", _msg("m1", 10)), ("c", _msg("m2", 20)), ("d", _msg("m3", 30))])
    a.fixar_mensagem("m1", "c", True, ts=100, duracao=50)     # vence em 150
    a.fixar_mensagem("m2", "c", True, ts=100, duracao=0)      # sem prazo conhecido
    a.fixar_mensagem("m3", "d", True, ts=100, duracao=500)
    assert [m["body"] for m in a.fixadas_de(["c"], 120)] == ["m1", "m2"]
    assert [m["body"] for m in a.fixadas_de(["c"], 200)] == ["m2"]
    assert [m["body"] for m in a.fixadas_de(["c", "d"], 200)] == ["m2", "m3"]
    a.fixar_mensagem("m2", "c", False)
    assert a.fixadas_de(["c"], 200) == []
