"""Mensagens de sistema: avisos de grupo (gp2) e registros de chamada (call_log)
a partir de eventos de mentira, com um Armazem em memoria (sem DLL nem conta)."""
import sys
import types
from types import SimpleNamespace as N

import chamadas
import eventos_grupos as eg
import mensagens_de_sistema as ms
from store import Armazem

AGORA = 1700000100


def jid(usuario, servidor="s.whatsapp.net"):
    return N(User=usuario, Server=servidor)


def pessoa(j):
    return eg.msgmap.jid_wpp(eg.jid_texto(j))


def grupo(**campos):
    base = dict(JID=jid("120", "g.us"), Sender=jid("1"), Timestamp=AGORA - 5,
                Join=[], Leave=[], Promote=[], Demote=[])
    base.update(campos)
    return N(**base)


def avisos(**campos):
    return ms.avisos_de_grupo(grupo(**campos), pessoa, AGORA)


class Sessao:
    nome = "s1"
    eu = "9@s.whatsapp.net"
    lid = "99@lid"

    def __init__(self):
        self.bd = Armazem(":memory:")
        self.enviados = []

    def emitir(self, evento, dados):
        self.enviados.append((evento, dados))


# ------------------------------------------------------------- avisos de grupo
def test_nome_novo_vira_gp2_subject_com_o_nome_no_body():
    (w,) = avisos(Name=N(Name="  Familia "))
    assert (w["type"], w["subtype"], w["body"]) == ("gp2", "subject", "Familia")
    assert w["author"] == "1@c.us" and w["recipients"] == []
    assert w["to"] == w["from"] == "120@g.us" and w["isStatus"] is False
    assert w["t"] == w["timestamp"] == AGORA - 5 and w["fromMe"] is False
    assert w["id"]["remote"] == "120@g.us"
    assert w["id"]["_serialized"] == f"false_120@g.us_{w['id']['id']}_1@c.us"


def test_descricao_nova_e_apagada():
    (nova,) = avisos(Topic=N(Topic="regras", TopicDeleted=False))
    assert (nova["subtype"], nova["body"]) == ("description", "regras")
    (apagada,) = avisos(Topic=N(Topic="velha", TopicDeleted=True))
    assert apagada["body"] == ""


def test_ajustes_levam_on_off_no_body_e_booleano_no_value():
    locked, anuncio, efemera = avisos(
        Locked=N(isLocked=True), Announce=N(IsAnnounce=False),
        Ephemeral=N(IsEphemeral=True, DisappearingTimer=86400))
    assert (locked["subtype"], locked["body"], locked["value"]) == ("restrict", "on", True)
    assert (anuncio["subtype"], anuncio["body"], anuncio["value"]) == ("announce", "off", False)
    assert (efemera["subtype"], efemera["value"]) == ("ephemeral", True)


def test_membros_viram_um_aviso_por_acao():
    ws = avisos(Join=[jid("2"), jid("3")], Promote=[jid("4")], Demote=[jid("5")])
    assert [(w["subtype"], w["recipients"]) for w in ws] == [
        ("add", ["2@c.us", "3@c.us"]), ("promote", ["4@c.us"]), ("demote", ["5@c.us"])]
    assert all(w["author"] == "1@c.us" for w in ws)


def test_quem_sai_sozinho_e_leave_e_quem_e_tirado_por_outro_e_remove():
    (saiu,) = avisos(Sender=jid("2"), Leave=[jid("2")])
    assert saiu["subtype"] == "leave" and saiu["recipients"] == ["2@c.us"]
    assert "author" not in saiu
    (tirado,) = avisos(Sender=jid("1"), Leave=[jid("2")])
    assert (tirado["subtype"], tirado["author"]) == ("remove", "1@c.us")
    (sem_autor,) = avisos(Sender=None, Leave=[jid("2")])
    assert sem_autor["subtype"] == "leave"


def test_entrada_por_link_nao_tem_autor_e_lid_vira_numero():
    pares = {"99@lid": "5511@c.us"}

    def com_par(j):
        return pares.get(pessoa(j), pessoa(j))

    e = grupo(Sender=jid("99", "lid"), Join=[jid("99", "lid")], JoinReason="invite")
    (w,) = ms.avisos_de_grupo(e, com_par, AGORA)
    assert w["subtype"] == "add" and w["recipients"] == ["5511@c.us"]
    assert "author" not in w


def test_varias_mudancas_do_mesmo_evento_tem_ids_diferentes_e_repetir_da_o_mesmo():
    campos = dict(Name=N(Name="X"), Join=[jid("2")], Leave=[jid("3")])
    ids = [w["id"]["id"] for w in avisos(**campos)]
    assert len(set(ids)) == 3
    assert ids == [w["id"]["id"] for w in avisos(**campos)]
    assert all("_" not in i for i in ids)
    outro = [w["id"]["id"] for w in avisos(Name=N(Name="Y"))]
    assert outro[0] not in ids


def test_evento_de_fora_de_grupo_ou_sem_mudanca_nao_gera_aviso():
    assert avisos(JID=jid("5511")) == []
    assert avisos() == []
    assert ms.avisos_de_grupo(N(), pessoa, AGORA) == []
    assert avisos(Name=N(Name="  ")) == []


def test_evento_sem_data_usa_a_hora_de_agora():
    (w,) = avisos(Timestamp=0, Name=N(Name="X"))
    assert w["t"] == AGORA


# ------------------------------------------------------- guardar e emitir
def test_guardar_e_emitir_guarda_emite_e_nao_soma_nao_lida():
    s = Sessao()
    (w,) = avisos(Name=N(Name="X"))
    assert ms.guardar_e_emitir(s, "120@g.us", w, AGORA)
    assert s.bd.mensagens("120@g.us") == [w]
    assert s.bd.nao_lidas("120@g.us") == 0
    assert s.bd.conversas()[0]["ts"] == w["t"]
    evento, dados = s.enviados[0]
    assert evento == "received-message" and dados["session"] == "s1"
    assert dados["response"] == w            # recente: sem a marca de historico


def test_o_mesmo_evento_duas_vezes_nao_duplica_nem_emite_de_novo():
    s = Sessao()
    (w,) = avisos(Name=N(Name="X"))
    assert ms.guardar_e_emitir(s, "120@g.us", w, AGORA)
    assert not ms.guardar_e_emitir(s, "120@g.us", w, AGORA)
    assert len(s.enviados) == 1 and len(s.bd.mensagens("120@g.us")) == 1


def test_aviso_velho_sai_marcado_como_historico_mas_guardado_sem_a_marca():
    s = Sessao()
    (w,) = ms.avisos_de_grupo(grupo(Timestamp=AGORA - 3600, Name=N(Name="X")), pessoa, AGORA)
    ms.guardar_e_emitir(s, "120@g.us", w, AGORA)
    assert s.enviados[0][1]["response"]["isMdHistoryMsg"] is True
    assert "isMdHistoryMsg" not in s.bd.mensagens("120@g.us")[0]


def test_sistema_nao_conta_como_ultima_recebida_nem_a_ler():
    s = Sessao()
    real = {"id": {"fromMe": False, "remote": "120@g.us", "id": "REAL", "_serialized": "x"},
            "fromMe": False, "t": AGORA - 20, "type": "chat", "body": "oi", "from": "1@c.us"}
    s.bd.mensagem("120@g.us", real)
    s.bd.ao_chegar("120@g.us", real["t"], False)
    (w,) = avisos(Name=N(Name="X"))
    ms.guardar_e_emitir(s, "120@g.us", w, AGORA)
    assert s.bd.ultima_recebida("120@g.us") == "REAL"
    assert [x[0] for x in s.bd.recebidas_a_ler("120@g.us")] == ["REAL"]


# -------------------------------------------------------------- chamadas
def no(tag="", attrs=None, filhos=()):
    return N(Tag=tag, Attrs=[N(name=k, text=v) for k, v in (attrs or {}).items()],
             Nodes=list(filhos))


def fim(id_chamada="C1", motivo="timeout", criador=None, alternativo=None, dados=None):
    meta = N(callID=id_chamada, timestamp=AGORA - 3, callCreator=criador or jid("5511"),
             callCreatorAlt=alternativo or jid(""))
    return N(basicCallMeta=meta, reason=motivo, data=dados or no("terminate"))


def terminar(s, e, tocava=True):
    return chamadas.registrar_fim(s, e, chamadas.evento_de_fim(e, AGORA), tocava, AGORA)


def test_desfechos():
    assert ms.desfecho_da_chamada("NOT_ANSWERED", False) == "Missed"
    assert ms.desfecho_da_chamada("REJECTED", False) == "Rejected"
    assert ms.desfecho_da_chamada("HANDLED_REMOTELY", False) == "AcceptedElsewhere"
    assert ms.desfecho_da_chamada("ENDED", True) == "Canceled"
    assert ms.desfecho_da_chamada("ENDED", False) is None


def test_chamada_perdida_vira_call_log_com_o_id_da_chamada():
    s = Sessao()
    assert terminar(s, fim())
    (w,) = s.bd.mensagens("5511@s.whatsapp.net")
    assert (w["type"], w["callOutcome"], w["finalCallOutcome"]) == ("call_log", "Missed", "Missed")
    assert w["id"]["id"] == "C1" and w["id"]["_serialized"] == "false_5511@c.us_C1"
    assert w["isVideoCall"] is False and w["fromMe"] is False
    assert w["callCreator"] == "5511@c.us" and w["t"] == AGORA - 3
    assert s.bd.nao_lidas("5511@s.whatsapp.net") == 0
    assert s.enviados[0][0] == "received-message"
    assert s.bd.mensagem_wpp("C1") == w          # o cliente a rele por message-by-id


def test_chamada_de_video_recusada_e_a_mesma_chamada_so_registra_uma_vez():
    s = Sessao()
    e = fim(motivo="reject", dados=no("terminate", filhos=[no("video")]))
    assert terminar(s, e, tocava=False)
    assert not terminar(s, e, tocava=False)
    (w,) = s.bd.mensagens("5511@s.whatsapp.net")
    assert w["callOutcome"] == "Rejected" and w["isVideoCall"] is True
    assert len(s.enviados) == 1


def test_fim_sem_motivo_de_chamada_que_nao_tocava_aqui_nao_registra():
    s = Sessao()
    assert not terminar(s, fim(motivo=""), tocava=False)
    assert s.enviados == [] and s.bd.conversas() == []


def test_chamada_de_grupo_vai_para_o_grupo_com_quem_ligou():
    s = Sessao()
    e = fim(dados=no("terminate", {"group-jid": "123-4@g.us"}))
    assert terminar(s, e)
    (w,) = s.bd.mensagens("123-4@g.us")
    assert w["to"] == "123-4@g.us" and w["callCreator"] == "5511@c.us"
    assert w["id"]["_serialized"] == "false_123-4@g.us_C1_5511@c.us"
    assert "author" not in w     # o cliente nomeia quem ligou por callCreator


def test_chamada_minha_de_outro_aparelho_nao_e_registrada():
    s = Sessao()
    assert not terminar(s, fim(criador=jid("9")))
    assert not terminar(s, fim(criador=jid("99", "lid")))
    assert s.enviados == []


def test_conversa_da_chamada_segue_a_forma_que_ja_tem_conversa():
    s = Sessao()
    meta = fim(criador=jid("77", "lid"), alternativo=jid("5511")).basicCallMeta
    assert chamadas.conversa_da_chamada(s.bd, meta, "") == "5511@s.whatsapp.net"
    s.bd.conversa("77@lid")
    assert chamadas.conversa_da_chamada(s.bd, meta, "") == "77@lid"
    vazio = N(callCreator=jid(""), callCreatorAlt=jid(""))
    assert chamadas.conversa_da_chamada(s.bd, vazio, "") == ""


# ------------------------------------------- ligacao aos manipuladores (sem neonize)
class _Cli:
    def __init__(self):
        self.manipuladores = {}

    def event(self, tipo):
        def liga(f):
            self.manipuladores[tipo] = f
            return f
        return liga

    def get_pn_from_lid(self, j):
        raise KeyError(j.User)


def test_grupo_mudou_emite_groups_update_e_o_aviso_gp2():
    s = Sessao()
    eg.grupo_mudou(_Cli(), s, grupo(Name=N(Name="Novo")), agora=AGORA)
    assert [e for e, _ in s.enviados] == ["groups.update", "received-message"]
    assert s.enviados[1][1]["response"]["subtype"] == "subject"
    eg.grupo_mudou(_Cli(), s, grupo(Name=N(Name="Novo")), agora=AGORA)   # repetido
    assert [e for e, _ in s.enviados].count("received-message") == 1


def test_grupo_mudou_sobrevive_a_sessao_sem_armazem():
    s = N(nome="s1", emitir=lambda *a: None)
    eg.grupo_mudou(_Cli(), s, grupo(Name=N(Name="Novo")), agora=AGORA)


def _eventos_falsos(monkeypatch):
    eventos = types.ModuleType("neonize.events")
    eventos.CallOfferEv = type("CallOfferEv", (), {})
    eventos.CallAcceptEv = type("CallAcceptEv", (), {})
    eventos.CallTerminateEv = type("CallTerminateEv", (), {})
    pacote = types.ModuleType("neonize")
    pacote.events = eventos
    monkeypatch.setitem(sys.modules, "neonize", pacote)
    monkeypatch.setitem(sys.modules, "neonize.events", eventos)
    return eventos


def test_o_fim_de_chamada_registra_pelo_manipulador(monkeypatch):
    eventos = _eventos_falsos(monkeypatch)
    cli, s = _Cli(), Sessao()
    chamadas.registrar_eventos(cli, s)
    s.chamadas_tocando.lembrar("C1", jid("5511"), 1)
    cli.manipuladores[eventos.CallTerminateEv](cli, fim(motivo=""))
    (w,) = s.bd.mensagens("5511@s.whatsapp.net")
    assert w["callOutcome"] == "Canceled"
    assert [e for e, _ in s.enviados] == ["incomingcall", "callstate", "received-message"]
    cli.manipuladores[eventos.CallTerminateEv](cli, N())      # quebrado: nao levanta


def test_accept_seguido_do_fim_registra_atendida_em_outro_aparelho_pelo_manipulador(monkeypatch):
    eventos = _eventos_falsos(monkeypatch)
    cli, s = _Cli(), Sessao()
    chamadas.registrar_eventos(cli, s)
    s.chamadas_tocando.lembrar("C1", jid("5511"), 1)
    accept = N(basicCallMeta=N(callID="C1", timestamp=AGORA - 33, callCreator=jid("5511"),
                               callCreatorAlt=jid("")))
    cli.manipuladores[eventos.CallAcceptEv](cli, accept)
    cli.manipuladores[eventos.CallAcceptEv](cli, N())         # quebrado: nao levanta
    cli.manipuladores[eventos.CallTerminateEv](cli, fim(motivo=""))
    (w,) = s.bd.mensagens("5511@s.whatsapp.net")
    assert w["callOutcome"] == "AcceptedElsewhere" and w["callDuration"] == 30
    callstate = [d for e, d in s.enviados if e == "callstate"][0]
    assert callstate["data"]["state"] == "HANDLED_REMOTELY"


def test_chamada_atendida_no_celular_nao_vira_perdida_e_leva_a_duracao():
    """Um fim sem motivo depois de um CallAccept era gravado como "Canceled", que
    o cliente conta como perdida, embora o usuario tivesse atendido no celular."""
    s = Sessao()
    e = fim(motivo="")
    dados = chamadas.refinar_fim(chamadas.evento_de_fim(e, AGORA), (AGORA - 33, AGORA - 33))
    assert chamadas.registrar_fim(s, e, dados, True, AGORA)
    (w,) = s.bd.mensagens("5511@s.whatsapp.net")
    assert (w["callOutcome"], w["finalCallOutcome"]) == ("AcceptedElsewhere", "AcceptedElsewhere")
    assert w["callDuration"] == 30                  # (AGORA - 3) - (AGORA - 33)


def test_fim_sem_motivo_e_sem_accept_continua_cancelada():
    s = Sessao()
    assert terminar(s, fim(motivo=""), tocava=True)
    (w,) = s.bd.mensagens("5511@s.whatsapp.net")
    assert (w["callOutcome"], w["callDuration"]) == ("Canceled", 0)
