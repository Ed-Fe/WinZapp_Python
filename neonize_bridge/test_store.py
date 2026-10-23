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
