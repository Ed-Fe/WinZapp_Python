"""Uma conversa guardada sob o numero ou o LID e achada pelos dois (conversas_alias.py)."""
from types import SimpleNamespace as N

import conversas_alias as ca
from store import Armazem

PN = "5511999990000@s.whatsapp.net"
LID = "111222333@lid"


def _existe(*guardadas):
    return lambda chave: chave in guardadas


def test_chaves_acha_a_conversa_pelo_outro_endereco():
    mapa = {PN: LID, LID: PN}
    # guardada pelo LID, pedida pelo numero (e o contrario)
    assert ca.chaves_da_conversa(_existe(LID), PN, lambda j: mapa.get(j, "")) == [LID]
    assert ca.chaves_da_conversa(_existe(PN), LID, lambda j: mapa.get(j, "")) == [PN]


def test_chaves_devolve_os_dois_enderecos_com_o_pedido_primeiro():
    mapa = {PN: LID, LID: PN}
    achadas = ca.chaves_da_conversa(_existe(PN, LID), LID, lambda j: mapa.get(j, ""))
    assert achadas == [LID, PN]


def test_chaves_aceita_a_forma_c_us_e_a_forma_legada_guardada():
    assert ca.chaves_da_conversa(_existe(PN), "5511999990000@c.us", lambda j: "") == [PN]
    assert ca.chaves_da_conversa(_existe("5511999990000@c.us"), PN, lambda j: "") == [
        "5511999990000@c.us"]


def test_chaves_sem_conversa_ou_sem_mapeamento_e_vazio():
    assert ca.chaves_da_conversa(_existe(), PN, lambda j: LID) == []
    assert ca.chaves_da_conversa(_existe(LID), PN, lambda j: "") == []


def test_grupo_nao_pergunta_pelo_outro_endereco():
    perguntas = []
    assert ca.chaves_da_conversa(_existe("1203@g.us"), "1203@g.us",
                                 lambda j: perguntas.append(j) or "") == ["1203@g.us"]
    assert perguntas == []


class _Jid:
    def __init__(self, usuario, servidor):
        self.User, self.Server = usuario, servidor


class _Neonize:
    def get_lid_from_pn(self, jid):
        return _Jid("111222333", "lid") if jid.User == "5511999990000" else _Jid("", "")

    def get_pn_from_lid(self, jid):
        return _Jid("5511999990000", "s.whatsapp.net") if jid.User == "111222333" else _Jid("", "")


class _Sessao(ca.AliasDeConversa):
    def __init__(self, bd, cliente, status="CONNECTED"):
        self.bd, self.cliente, self.status = bd, cliente, status


def test_sessao_acha_a_conversa_pelo_mapeamento_do_neonize(tmp_path):
    bd = Armazem(str(tmp_path / "x.db"))
    bd.conversa(LID, name="Ana")
    s = _Sessao(bd, _Neonize())
    assert s.chat_existente("5511999990000@c.us") == LID
    assert s.chaves_da_conversa(PN) == [LID]
    assert s.chat_existente("999@s.whatsapp.net") == ""


def test_sessao_desconectada_so_acha_a_chave_exata(tmp_path):
    bd = Armazem(str(tmp_path / "x.db"))
    bd.conversa(LID)
    assert _Sessao(bd, None, "CLOSED").chat_existente(PN) == ""
    assert _Sessao(bd, None, "CLOSED").chat_existente(LID) == LID
