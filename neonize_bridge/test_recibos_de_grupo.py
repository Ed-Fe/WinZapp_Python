"""Recibos de grupo: o ack de uma mensagem nossa num grupo so avanca quando TODOS
os destinatarios chegaram la. Funcoes puras, a Sessao de verdade (manipulador do
ReceiptEv, message-ack e get-messages) contra um cliente neonize falso.

Nada aqui fala com uma conta real; a regra do tique azul num grupo de verdade
nao foi verificada (COBERTURA.md)."""
import sys
import time
import types
from types import SimpleNamespace as N

import pytest

import recibos_de_grupo as rg
import server
from recibos_de_grupo import Membro, avaliar, chaves_de, destinatarios
from test_conversas_servidor import EU, Falso, _abrir, _msg, sessoes  # noqa: F401

GRUPO = "120363000000000001@g.us"
A = "5511911110001@s.whatsapp.net"
B = "5511911110002@s.whatsapp.net"
C = "5511911110003@s.whatsapp.net"
LID_A, LID_B, LID_C = "1001@lid", "1002@lid", "1003@lid"
MEU_LID = "999@lid"


def membro(fone, lid="", desde=0):
    return rg.membro_de_endereco(fone, lid, desde)


def trio():
    return [membro(A, LID_A), membro(B, LID_B), membro(C, LID_C)]


def r(quem, etapa, ts=1000):
    return (quem, etapa, ts)


AGORA = 1000 + 60       # um minuto depois dos recibos de ts=1000: dentro da folga


# ------------------------------------------------------------------ enderecos
def test_chaves_juntam_as_formas_do_mesmo_endereco():
    assert chaves_de("5511911110001@c.us") == chaves_de("5511911110001:7@s.whatsapp.net")
    assert chaves_de("5511911110001@c.us") != chaves_de(LID_A)


def test_chaves_do_celular_brasileiro_com_e_sem_o_nono_digito():
    assert chaves_de("5511911110001@c.us") & chaves_de("551111110001@c.us")


def test_chaves_ignoram_endereco_vazio_ou_quebrado():
    assert chaves_de("", None, "semarroba", "@lid") == frozenset()


def test_lid_nao_e_tratado_como_telefone():
    # os digitos de um LID nao sao um numero: 5511... em @lid nao cruza com o @c.us
    assert not (chaves_de("5511911110001@lid") & chaves_de("5511911110001@c.us"))


# ------------------------------------------------------------------ avaliacao
def test_um_recibo_de_leitura_em_tres_nao_marca_a_mensagem_como_lida():
    # o bug: o primeiro leitor virava ack 3 para o grupo inteiro
    r_ = avaliar(trio(), [r(A, "delivered"), r(A, "read")], AGORA)
    assert r_.ack == 1 and (r_.entregues, r_.lidos, r_.esperados) == (1, 1, 3)


def test_entregue_so_quando_todos_receberam():
    assert avaliar(trio(), [r(A, "delivered"), r(B, "delivered")], AGORA).ack == 1
    assert avaliar(trio(), [r(A, "delivered"), r(B, "delivered"),
                            r(C, "delivered")], AGORA).ack == 2


def test_lida_so_quando_todos_leram():
    entregues = [r(p, "delivered") for p in (A, B, C)]
    assert avaliar(trio(), entregues + [r(A, "read"), r(B, "read")], AGORA).ack == 2
    assert avaliar(trio(), entregues + [r(p, "read") for p in (A, B, C)], AGORA).ack == 3


def test_recibo_de_leitura_sem_o_de_entrega_conta_como_entregue():
    assert avaliar(trio(), [r(p, "read") for p in (A, B, C)], AGORA).ack == 3


def test_ouvida_so_quando_todos_ouviram():
    base = [r(p, "read") for p in (A, B, C)]
    assert avaliar(trio(), base + [r(A, "played"), r(B, "played")], AGORA).ack == 3
    assert avaliar(trio(), base + [r(p, "played") for p in (A, B, C)], AGORA).ack == 4


def test_voz_ouvida_por_todos_sem_recibo_de_leitura_e_ouvida():
    # 'played' implica 'read' e 'delivered'
    assert avaliar(trio(), [r(p, "played") for p in (A, B, C)], AGORA).ack == 4


def test_voz_ouvida_por_um_so_nao_vira_ouvida_nem_lida():
    r_ = avaliar(trio(), [r(A, "played")], AGORA)
    assert r_.ack == 1 and r_.ouvidos == 1


def test_sem_destinatarios_o_ack_fica_em_enviada():
    assert avaliar([], [r(A, "read")], AGORA).ack == 1


def test_recibo_de_quem_nao_e_membro_e_ignorado():
    de_fora = "5599900000000@s.whatsapp.net"
    r_ = avaliar([membro(A, LID_A)], [r(de_fora, "read"), r(de_fora, "delivered")], AGORA)
    assert r_.ack == 1 and r_.entregues == 0 and r_.linhas == []


def test_etapa_desconhecida_e_ignorada():
    assert avaliar([membro(A)], [r(A, "sent")], AGORA).ack == 1


def test_a_mesma_pessoa_por_dois_enderecos_conta_uma_vez():
    # o recibo de leitura veio pelo LID, o de entrega pelo numero
    r_ = avaliar([membro(A, LID_A)], [r(A, "delivered"), r(LID_A, "read")], AGORA)
    assert r_.ack == 3 and r_.esperados == 1 and r_.linhas[0][0] == "5511911110001@c.us"
    assert {l[0] for l in r_.linhas} == {"5511911110001@c.us"}


def test_o_resolvedor_cruza_o_lid_com_o_numero_quando_a_lista_so_tem_um():
    so_numero = [membro(A)]
    assert avaliar(so_numero, [r(LID_A, "read")], AGORA).ack == 1
    assert avaliar(so_numero, [r(LID_A, "read")], AGORA,
                   resolver=lambda q: A if q == LID_A else "").ack == 3


def test_o_resolvedor_que_falha_nao_derruba_a_avaliacao():
    def quebra(_):
        raise RuntimeError("sem cliente")
    assert avaliar([membro(A)], [r(LID_A, "read")], AGORA, resolver=quebra).ack == 1


def test_numero_brasileiro_com_e_sem_o_nono_digito_e_a_mesma_pessoa():
    r_ = avaliar([membro("5511911110001@s.whatsapp.net")],
                 [r("551111110001@c.us", "read")], AGORA)
    assert r_.ack == 3


# ------------------------------------------------------------------ destinatarios
def test_a_propria_conta_nao_e_destinatario_pelo_numero_nem_pelo_lid():
    eu = chaves_de(EU, MEU_LID)
    todos = trio() + [membro(EU), membro("5500000000@c.us", MEU_LID)]
    assert [m.id for m in destinatarios(todos, eu)] == [m.id for m in trio()]
    assert destinatarios([membro("x", MEU_LID)], eu) == []      # so o LID dele


def test_quem_entrou_depois_da_mensagem_nao_e_destinatario():
    novo = membro(C, LID_C, desde=2000)
    assert [m.id for m in destinatarios([membro(A), novo], frozenset(), 1500)] == [
        "5511911110001@c.us"]
    assert len(destinatarios([membro(A), novo], frozenset(), 2500)) == 2
    # entrada que a ponte nao viu (desde 0) ou mensagem sem hora: conta
    assert len(destinatarios([membro(A), membro(C, desde=0)], frozenset(), 1500)) == 2
    assert len(destinatarios([membro(A), novo], frozenset(), 0)) == 2


def test_quem_entrou_depois_nao_segura_o_ack():
    esperados = destinatarios([membro(A), membro(B, desde=5000)], frozenset(), 1500)
    assert avaliar(esperados, [r(A, "read")], AGORA).ack == 3


# ------------------------------------------------------------- folga sem leitura
def folga(recibos, agora, **kw):
    return avaliar(trio(), recibos, agora, graca=600, **kw)


def test_quem_so_entregou_nao_segura_o_lida_depois_da_folga():
    recibos = ([r(p, "delivered", 1000) for p in (A, B, C)]
               + [r(A, "read", 1010), r(B, "read", 1020)])
    assert folga(recibos, 1000 + 599).ack == 2          # ainda dentro da folga
    assert folga(recibos, 1000 + 600).ack == 3          # C so entregou ha 10 minutos


def test_a_folga_conta_a_partir_da_entrega_de_cada_um():
    recibos = [r(A, "read", 1000), r(B, "read", 1000),
               r(C, "delivered", 5000)]                 # entregou agora ha pouco
    assert folga(recibos, 5100).ack == 2
    assert folga(recibos, 5600).ack == 3


def test_a_folga_nao_vale_sem_nenhuma_leitura_de_verdade():
    recibos = [r(p, "delivered", 1000) for p in (A, B, C)]
    assert folga(recibos, 10_000).ack == 2


def test_a_folga_nao_perdoa_quem_nem_entregou():
    recibos = [r(A, "read", 1000), r(B, "read", 1000)]
    assert folga(recibos, 10_000).ack == 1              # C pode estar sem sinal


def test_a_folga_nao_vale_para_ouvida():
    recibos = [r(A, "played", 1000), r(B, "played", 1000), r(C, "delivered", 1000)]
    assert folga(recibos, 10_000).ack == 3
    assert folga(recibos + [r(C, "played", 11_000)], 11_000).ack == 4


def test_recibo_sem_hora_nao_dispara_a_folga():
    recibos = [r(A, "read", 1000), r(B, "read", 1000), r(C, "delivered", 0)]
    assert folga(recibos, 10_000_000).ack == 2


def test_a_folga_padrao_e_dez_minutos():
    assert rg.GRACA_SEM_LEITURA == 600


# -------------------------------------------------------------------- elenco
def test_elenco_guarda_o_desde_de_quem_ja_era_conhecido():
    e = rg.Elenco()
    e.definir(GRUPO, [membro(A, LID_A)], 10)
    e.entrou(GRUPO, membro(B, LID_B, desde=500))
    e.definir(GRUPO, [membro(A, LID_A), membro(B, LID_B)], 20)
    assert {m.id: m.desde for m in e.membros(GRUPO)}["5511911110002@c.us"] == 500


def test_elenco_entrou_e_saiu():
    e = rg.Elenco()
    e.entrou(GRUPO, membro(A))                      # grupo nao carregado: nada
    assert e.membros(GRUPO) is None
    e.definir(GRUPO, [membro(A, LID_A), membro(B, LID_B)], 10)
    e.entrou(GRUPO, membro(C, LID_C, desde=50))
    e.entrou(GRUPO, membro(C, LID_C, desde=60))     # nao duplica
    assert len(e.membros(GRUPO)) == 3
    e.saiu(GRUPO, chaves_de(LID_A))                 # sai so pelo LID
    assert [m.id for m in e.membros(GRUPO)] == ["5511911110002@c.us", "5511911110003@c.us"]


def test_elenco_vence_depois_da_validade_e_invalida():
    e = rg.Elenco(validade=100)
    assert e.velho(GRUPO, 0)
    e.definir(GRUPO, [membro(A)], 10)
    assert not e.velho(GRUPO, 109) and e.velho(GRUPO, 110)
    e.invalidar(GRUPO)
    assert e.membros(GRUPO) is None


def test_membro_de_participante_prefere_o_numero_e_guarda_os_dois_enderecos():
    def texto(j):
        return f"{j.User}@{j.Server}" if j is not None and j.User else ""
    p = N(JID=N(User="1001", Server="lid"), LID=N(User="1001", Server="lid"),
          PhoneNumber=N(User="5511911110001", Server="s.whatsapp.net"))
    m = rg.membro_de_participante(p, texto)
    assert m.id == "5511911110001@c.us"
    assert m.chaves >= chaves_de(A) | chaves_de(LID_A)
    sem_numero = N(JID=N(User="1001", Server="lid"), LID=N(User="1001", Server="lid"),
                   PhoneNumber=N(User="", Server=""))
    assert rg.membro_de_participante(sem_numero, texto).id == "1001@lid"
    assert rg.membro_de_participante(N(JID=None, LID=None, PhoneNumber=None), texto) is None


def jid_n(endereco):
    usuario, _, servidor = endereco.partition("@")
    return N(User=usuario, Server=servidor)


def texto_n(j):
    return f"{j.User}@{j.Server}" if j is not None and j.User else ""


def test_aplicar_evento_registra_entrada_com_a_hora_e_saida():
    e = rg.Elenco()
    e.definir(GRUPO, [membro(A, LID_A), membro(B, LID_B)], 10)
    evento = N(Join=[jid_n(C)], Leave=[jid_n(B)], Timestamp=3000)
    rg.aplicar_evento(e, GRUPO, evento, 9999, texto_n,
                      lambda q: LID_C if q == C else "")
    por_id = {m.id: m for m in e.membros(GRUPO)}
    assert set(por_id) == {"5511911110001@c.us", "5511911110003@c.us"}
    assert por_id["5511911110003@c.us"].desde == 3000
    assert por_id["5511911110003@c.us"].chaves >= chaves_de(LID_C)


def test_aplicar_evento_sem_hora_usa_o_agora_e_aguenta_resolvedor_que_falha():
    e = rg.Elenco()
    e.definir(GRUPO, [membro(A)], 10)

    def quebra(_):
        raise RuntimeError("x")
    rg.aplicar_evento(e, GRUPO, N(Join=[jid_n(C)], Leave=[]), 777, texto_n, quebra)
    assert {m.id: m.desde for m in e.membros(GRUPO)}["5511911110003@c.us"] == 777


# ------------------------------------------------- Sessao: manipulador do recibo
def participante(fone, lid):
    return N(JID=jid_n(lid), LID=jid_n(lid), PhoneNumber=jid_n(fone),
             IsAdmin=False, IsSuperAdmin=False)


def info_do_grupo(*pessoas):
    meu = N(JID=jid_n(MEU_LID), LID=jid_n(MEU_LID), PhoneNumber=jid_n(EU),
            IsAdmin=True, IsSuperAdmin=True)
    return N(JID=jid_n(GRUPO), Participants=[meu, *pessoas],
             GroupName=N(Name="Grupo"))


TRES = (participante(A, LID_A), participante(B, LID_B), participante(C, LID_C))


class ClienteDeGrupo(Falso):
    def __init__(self, info):
        super().__init__()
        self.info, self.consultas = info, 0

    def get_group_info(self, jid):
        self.consultas += 1
        if self.info is None:
            raise RuntimeError("sem rede")
        return self.info


class RegistroDeEventos:
    """O `cli.event` do neonize: guarda o manipulador de cada tipo de evento."""

    def __init__(self):
        self.manipuladores = {}

    def __call__(self, tipo):
        def guardar(funcao):
            self.manipuladores[tipo] = funcao
            return funcao
        return guardar

    def qr(self, funcao):
        return funcao


class EventosFalsos(types.ModuleType):
    def __getattr__(self, nome):
        if nome.startswith("__"):
            raise AttributeError(nome)
        tipo = type(nome, (), {})
        setattr(self, nome, tipo)
        return tipo


@pytest.fixture
def sessao(tmp_path, monkeypatch):
    """Uma Sessao de verdade, conectada, com o manipulador do ReceiptEv registrado
    por `_rodar` num NewClient falso."""
    import neonize
    import neonize.client
    registro = RegistroDeEventos()
    # a versao instalada do neonize pode nao ter todos os tipos de evento que a
    # ponte registra: um modulo falso entrega um tipo distinto para cada nome
    eventos = EventosFalsos("neonize.events")
    monkeypatch.setitem(sys.modules, "neonize.events", eventos)
    monkeypatch.setattr(neonize, "events", eventos, raising=False)

    class NewClientFalso:
        def __init__(self, *args, **kw):
            self.event = registro

        def connect(self):
            pass
    monkeypatch.setattr(neonize.client, "NewClient", NewClientFalso)
    s = server.Sessao("conta1", str(tmp_path / "conta1"), None, None)
    s.emitidos = []
    s.emitir = lambda evento, dados: s.emitidos.append((evento, dados))
    s._rodar(None, s.ciclo.abrir())
    import neonize.events as nev
    s.recibo = registro.manipuladores[nev.ReceiptEv]
    s.manipuladores = registro.manipuladores
    s.cliente = ClienteDeGrupo(info_do_grupo(*TRES))
    s.status, s.eu, s.lid = "CONNECTED", EU, MEU_LID
    s.emitidos.clear()
    s.bd.mensagem(GRUPO, _msg("M1", int(time.time()) - 5, GRUPO, de_mim=True))
    return s


TIPO = {"DELIVERED": 1, "READ": 4, "PLAYED": 6}


def receber(s, tipo, quem, mid="M1", ts=None, meu=False):
    s.recibo(s.cliente, N(
        Type=TIPO[tipo], MessageIDs=[mid], Timestamp=ts or int(time.time()),
        MessageSource=N(Chat=jid_n(GRUPO), Sender=jid_n(quem), IsFromMe=meu)))


def acks(s):
    return [d["ack"] for ev, d in s.emitidos if ev == "onack"]


def test_o_manipulador_do_grupo_so_emite_quando_todos_chegaram(sessao):
    receber(sessao, "DELIVERED", A)
    receber(sessao, "READ", A)              # um leitor so: nada de azul
    receber(sessao, "DELIVERED", B)
    assert acks(sessao) == []
    receber(sessao, "DELIVERED", C)
    assert acks(sessao) == [2]
    receber(sessao, "READ", B)
    assert acks(sessao) == [2]
    receber(sessao, "READ", C)
    assert acks(sessao) == [2, 3]


def test_o_onack_de_grupo_tem_o_formato_do_wppconnect(sessao):
    for quem in (A, B, C):
        receber(sessao, "DELIVERED", quem)
    _, dados = sessao.emitidos[-1]
    chat = "120363000000000001@g.us"
    assert dados == {"session": "conta1", "ack": 2, "to": chat, "from": chat,
                     "id": {"fromMe": True, "remote": chat, "id": "M1",
                            "_serialized": f"true_{chat}_M1"}}


def test_nunca_emite_ack_menor_ou_igual_ao_ja_emitido(sessao):
    for quem in (A, B, C):
        receber(sessao, "READ", quem)
    assert acks(sessao) == [3]
    receber(sessao, "DELIVERED", A)         # recibo atrasado, ja era 3
    receber(sessao, "READ", A)
    assert acks(sessao) == [3]


def test_recibo_de_leitura_antes_do_de_entrega_nao_volta_atras(sessao):
    receber(sessao, "READ", A)
    receber(sessao, "READ", B)
    receber(sessao, "DELIVERED", C)
    assert acks(sessao) == [2]
    receber(sessao, "READ", C)
    assert acks(sessao) == [2, 3]


def test_voz_em_grupo_so_vira_ouvida_quando_todos_ouviram(sessao):
    for quem in (A, B, C):
        receber(sessao, "READ", quem)
    receber(sessao, "PLAYED", A)
    receber(sessao, "PLAYED", B)
    assert acks(sessao) == [3]
    receber(sessao, "PLAYED", C)
    assert acks(sessao) == [3, 4]


def test_recibo_de_quem_nao_e_do_grupo_nao_conta(sessao):
    de_fora = "5599900000000@s.whatsapp.net"
    receber(sessao, "READ", de_fora)
    receber(sessao, "READ", A)
    receber(sessao, "READ", B)
    assert acks(sessao) == []
    receber(sessao, "READ", C)
    assert acks(sessao) == [3]


def test_recibo_pelo_lid_conta_como_a_pessoa_do_numero(sessao):
    receber(sessao, "READ", A)
    receber(sessao, "READ", LID_B)
    receber(sessao, "READ", C)
    assert acks(sessao) == [3]


def test_o_recibo_continua_guardado_por_participante(sessao):
    receber(sessao, "READ", A)
    receber(sessao, "READ", B)
    assert {q for q, _, _ in sessao.bd.recibos_de("M1")} == {"5511911110001@c.us",
                                                              "5511911110002@c.us"}


def test_membros_desconhecidos_nao_emitem_nada(sessao):
    sessao.cliente = ClienteDeGrupo(None)           # GetGroupInfo falha
    for quem in (A, B, C):
        receber(sessao, "READ", quem)
    assert acks(sessao) == []


def test_a_lista_de_membros_e_buscada_uma_vez_e_guardada(sessao):
    receber(sessao, "DELIVERED", A)
    receber(sessao, "DELIVERED", B)
    receber(sessao, "READ", A)
    assert sessao.cliente.consultas == 1


def test_a_busca_que_falha_e_tentada_de_novo_no_proximo_recibo(sessao):
    sessao.cliente.info = None
    receber(sessao, "DELIVERED", A)
    sessao.cliente.info = info_do_grupo(*TRES)
    for quem in (A, B, C):
        receber(sessao, "DELIVERED", quem)
    assert acks(sessao) == [2]


def test_a_propria_conta_nao_segura_o_ack(sessao):
    # a conta esta na lista de participantes; ela nunca manda recibo de si mesma
    for quem in (A, B, C):
        receber(sessao, "DELIVERED", quem)
    assert acks(sessao) == [2]
    assert sessao.eu not in [q for q, _, _ in sessao.bd.recibos_de("M1")]


def test_recibo_lido_no_meu_proprio_aparelho_nao_emite_ack_de_grupo(sessao):
    receber(sessao, "READ", A, meu=True)
    assert [ev for ev, _ in sessao.emitidos if ev == "onack"] == []


def test_conversa_de_uma_pessoa_continua_emitindo_o_primeiro_recibo(sessao):
    s = sessao
    s.recibo(s.cliente, N(Type=TIPO["READ"], MessageIDs=["M9"], Timestamp=int(time.time()),
                          MessageSource=N(Chat=jid_n(A), Sender=jid_n(A), IsFromMe=False)))
    assert acks(s) == [3]


# ------------------------------------------------- tamanho do grupo muda
def test_quem_saiu_do_grupo_deixa_de_ser_esperado(sessao):
    receber(sessao, "READ", A)
    receber(sessao, "READ", B)
    assert acks(sessao) == []
    import neonize.events as nev
    sessao.manipuladores[nev.GroupInfoEv](sessao.cliente, N(
        JID=jid_n(GRUPO), Join=[], Leave=[jid_n(C)], Promote=[], Demote=[],
        Name=None, Timestamp=int(time.time())))
    receber(sessao, "READ", A, ts=int(time.time()) + 1)     # qualquer recibo reavalia
    # o recibo de A ja existia: o INSERT OR IGNORE nao muda nada, mas a avaliacao
    # agora so espera por A e B
    assert acks(sessao) == [3]


def test_quem_entrou_depois_do_envio_nao_segura_o_ack(sessao):
    import neonize.events as nev
    novo = "5511911110004@s.whatsapp.net"
    receber(sessao, "READ", A)
    receber(sessao, "READ", B)
    sessao.manipuladores[nev.GroupInfoEv](sessao.cliente, N(
        JID=jid_n(GRUPO), Join=[jid_n(novo)], Leave=[], Promote=[], Demote=[],
        Name=None, Timestamp=int(time.time()) + 100))       # depois da mensagem
    receber(sessao, "READ", C)
    assert acks(sessao) == [3]


def test_quem_entrou_antes_do_envio_segura_o_ack(sessao):
    import neonize.events as nev
    novo = "5511911110004@s.whatsapp.net"
    sessao.membros_do_grupo(GRUPO)                           # carrega a lista
    sessao.manipuladores[nev.GroupInfoEv](sessao.cliente, N(
        JID=jid_n(GRUPO), Join=[jid_n(novo)], Leave=[], Promote=[], Demote=[],
        Name=None, Timestamp=int(time.time()) - 3600))
    for quem in (A, B, C):
        receber(sessao, "READ", quem)
    assert acks(sessao) == []
    receber(sessao, "READ", novo)
    assert acks(sessao) == [3]


def test_grupo_novo_chega_pelo_joinedgroupev_e_pelo_carregar_nomes(sessao):
    import neonize.events as nev
    sessao.cliente.info = None                  # sem rede: so a lista guardada vale
    sessao.manipuladores[nev.JoinedGroupEv](sessao.cliente, N(
        GroupInfo=info_do_grupo(participante(A, LID_A), participante(B, LID_B))))
    receber(sessao, "READ", A)
    receber(sessao, "READ", B)
    assert acks(sessao) == [3]


def test_carregar_nomes_guarda_os_membros_dos_grupos(sessao):
    sessao.cliente.info = None
    sessao.cliente.contact = N(get_all_contacts=lambda: [])
    sessao.cliente.get_joined_groups = lambda: [info_do_grupo(*TRES)]
    sessao._carregar_nomes()
    assert len(sessao.membros_do_grupo(GRUPO, buscar=False)) == 4   # os 3 e eu


# ------------------------------------------------- folga e reavaliacao
def test_a_folga_vence_na_proxima_avaliacao_sem_temporizador(sessao):
    agora = int(time.time())
    receber(sessao, "DELIVERED", A, ts=agora)
    receber(sessao, "READ", A, ts=agora)
    receber(sessao, "DELIVERED", B, ts=agora)
    receber(sessao, "READ", B, ts=agora)
    receber(sessao, "DELIVERED", C, ts=agora)           # C entregou e nunca le
    assert acks(sessao) == [2]
    assert sessao.ack_de_grupo_a_emitir(GRUPO, "M1", agora=agora + 599) is None
    assert sessao.ack_de_grupo_a_emitir(GRUPO, "M1", agora=agora + 601) == 3
    assert sessao.ack_de_grupo_a_emitir(GRUPO, "M1", agora=agora + 700) is None


# ------------------------------------------------- message-ack e get-messages
async def _ponte(tmp_path, sessoes, info=None):  # noqa: F811
    ponte = await _abrir(tmp_path, sessoes)
    ponte.s.cliente = ClienteDeGrupo(info if info is not None else info_do_grupo(*TRES))
    ponte.s.lid = MEU_LID
    ponte.s.bd.conversa(GRUPO)
    ponte.s.bd.mensagem(GRUPO, _msg("M1", int(time.time()) - 5, GRUPO, de_mim=True))
    return ponte


@pytest.mark.asyncio
async def test_message_ack_de_grupo_conta_quem_ainda_falta(tmp_path, sessoes):  # noqa: F811
    ponte = await _ponte(tmp_path, sessoes)
    try:
        bd = ponte.s.bd
        bd.recibo("M1", "5511911110001@c.us", "delivered", 10)
        bd.recibo("M1", "5511911110001@c.us", "read", 20)
        bd.recibo("M1", "5511911110002@c.us", "delivered", 15)
        status, corpo = await ponte.post("message-ack", {"messageId": f"true_{GRUPO}_M1"})
        assert status == 200
        resposta = corpo["response"]
        assert {p["id"] for p in resposta["participants"]} == {
            "5511911110001@c.us", "5511911110002@c.us"}
        assert resposta["deliveryRemaining"] == 1       # falta C
    finally:
        await ponte.http.close()


@pytest.mark.asyncio
async def test_message_ack_de_grupo_ignora_quem_nao_e_membro_e_junta_enderecos(tmp_path, sessoes):  # noqa: F811
    ponte = await _ponte(tmp_path, sessoes)
    try:
        bd = ponte.s.bd
        bd.recibo("M1", "5511911110001@c.us", "delivered", 10)
        bd.recibo("M1", LID_A, "read", 20)                  # a mesma pessoa, pelo LID
        bd.recibo("M1", "5599900000000@c.us", "read", 30)   # nao e do grupo
        status, corpo = await ponte.post("message-ack", {"messageId": f"true_{GRUPO}_M1"})
        resposta = corpo["response"]
        assert resposta["participants"] == [
            {"id": "5511911110001@c.us", "deliveredAt": 10, "readAt": 20}]
        assert resposta["deliveryRemaining"] == 2
    finally:
        await ponte.http.close()


@pytest.mark.asyncio
async def test_message_ack_de_grupo_nunca_devolve_restante_negativo(tmp_path, sessoes):  # noqa: F811
    # o grupo encolheu: mais gente com recibo do que destinatarios agora -> 0
    ponte = await _ponte(tmp_path, sessoes, info_do_grupo(TRES[0]))
    try:
        ponte.s.bd.recibo("M1", "5511911110001@c.us", "read", 20)
        _, corpo = await ponte.post("message-ack", {"messageId": f"true_{GRUPO}_M1"})
        assert corpo["response"]["deliveryRemaining"] == 0
    finally:
        await ponte.http.close()


@pytest.mark.asyncio
async def test_message_ack_de_conversa_de_uma_pessoa_continua_com_zero(tmp_path, sessoes):  # noqa: F811
    ponte = await _ponte(tmp_path, sessoes)
    try:
        ponte.s.bd.mensagem(A, _msg("M2", 100, A, de_mim=True))
        ponte.s.bd.recibo("M2", A, "read", 20)
        _, corpo = await ponte.post("message-ack", {"messageId": f"true_{A}_M2"})
        assert corpo["response"]["deliveryRemaining"] == 0
    finally:
        await ponte.http.close()


@pytest.mark.asyncio
async def test_message_ack_depois_da_folga_nao_muda_os_participantes(tmp_path, sessoes):  # noqa: F811
    ponte = await _ponte(tmp_path, sessoes)
    try:
        for quem in ("5511911110001@c.us", "5511911110002@c.us", "5511911110003@c.us"):
            ponte.s.bd.recibo("M1", quem, "delivered", 10)
        _, corpo = await ponte.post("message-ack", {"messageId": f"true_{GRUPO}_M1"})
        assert len(corpo["response"]["participants"]) == 3
        assert corpo["response"]["deliveryRemaining"] == 0
    finally:
        await ponte.http.close()


@pytest.mark.asyncio
async def test_get_messages_de_grupo_nao_mostra_lida_com_um_leitor_so(tmp_path, sessoes):  # noqa: F811
    ponte = await _ponte(tmp_path, sessoes)
    try:
        await asyncio_em_thread(ponte.s.membros_do_grupo, GRUPO)    # carrega a lista
        ponte.s.bd.recibo("M1", "5511911110001@c.us", "read", 20)
        _, corpo = await ponte.get(f"get-messages/{GRUPO}?count=10")
        assert [m["ack"] for m in corpo["response"]] == [1]
        for quem in ("5511911110002@c.us", "5511911110003@c.us"):
            ponte.s.bd.recibo("M1", quem, "read", 21)
        _, corpo = await ponte.get(f"get-messages/{GRUPO}?count=10")
        assert [m["ack"] for m in corpo["response"]] == [3]
        _, corpo = await ponte.get(f"message-by-id/true_{GRUPO}_M1")
        assert corpo["response"]["data"]["ack"] == 3
    finally:
        await ponte.http.close()


@pytest.mark.asyncio
async def test_get_messages_de_grupo_nao_desce_abaixo_do_ack_ja_emitido(tmp_path, sessoes):  # noqa: F811
    ponte = await _ponte(tmp_path, sessoes)
    try:
        await asyncio_em_thread(ponte.s.membros_do_grupo, GRUPO)
        for quem in ("5511911110001@c.us", "5511911110002@c.us", "5511911110003@c.us"):
            ponte.s.bd.recibo("M1", quem, "read", 21)
        assert ponte.s.ack_de_grupo_a_emitir(GRUPO, "M1") == 3
        ponte.s.elenco_mudou(N(JID=jid_n(GRUPO), Leave=[], Timestamp=1,
                               Join=[jid_n("5511911110009@s.whatsapp.net")]))
        # o recem-chegado (hora 1: antes da mensagem) passaria a ser esperado
        _, corpo = await ponte.get(f"get-messages/{GRUPO}?count=10")
        assert [m["ack"] for m in corpo["response"]] == [3]
    finally:
        await ponte.http.close()


@pytest.mark.asyncio
async def test_get_messages_de_grupo_com_membros_desconhecidos_fica_em_enviada(tmp_path, sessoes):  # noqa: F811
    ponte = await _ponte(tmp_path, sessoes)
    try:
        ponte.s.bd.recibo("M1", "5511911110001@c.us", "read", 20)
        _, corpo = await ponte.get(f"get-messages/{GRUPO}?count=10")
        assert [m["ack"] for m in corpo["response"]] == [1]
    finally:
        await ponte.http.close()


async def asyncio_em_thread(funcao, *args):
    import asyncio
    return await asyncio.get_running_loop().run_in_executor(None, funcao, *args)
