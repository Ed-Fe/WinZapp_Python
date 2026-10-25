"""Chamadas: eventos de oferta/fim no formato do cliente e a recusa."""
import asyncio
from types import SimpleNamespace as N

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import chamadas


def jid(usuario, servidor="s.whatsapp.net"):
    return N(User=usuario, Server=servidor)


def no(tag="", attrs=None, filhos=()):
    return N(Tag=tag, Attrs=[N(name=k, text=v) for k, v in (attrs or {}).items()],
             Nodes=list(filhos))


def meta(id_chamada="C1", criador=None, alternativo=None, ts=1700000000):
    return N(callID=id_chamada, timestamp=ts, callCreator=criador or jid("5511999"),
             callCreatorAlt=alternativo or jid(""))


def oferta(dados=None, **kw):
    return N(basicCallMeta=meta(**kw), data=dados or no("offer", filhos=[no("audio")]))


def test_atributo_e_tag_procuram_nos_descendentes():
    raiz = no("call", {"t": "1"}, [no("offer", {"group-jid": "123@g.us"}, [no("video")])])
    assert chamadas.atributo(raiz, "group-jid") == "123@g.us"
    assert chamadas.atributo(raiz, "nada") == ""
    assert chamadas.tem_tag(raiz, "video")
    assert not chamadas.tem_tag(no("offer", filhos=[no("audio")]), "video")


def test_oferta_de_voz_vira_o_evento_que_o_cliente_le():
    dados = chamadas.evento_de_oferta(oferta(), 1700000005.7)
    assert dados == {
        "event": "offer", "state": "INCOMING_RING", "peerJid": "5511999@c.us",
        "id": "C1", "isVideo": False, "isGroup": False, "groupJid": "",
        "timestamp": 1700000000, "observedAt": 1700000005,
        "offerReceivedWhileOffline": False}


def test_oferta_de_video_de_grupo_e_de_fila():
    no_oferta = no("offer", {"group-jid": "123-456@g.us", "offline": "1"}, [no("video")])
    dados = chamadas.evento_de_oferta(oferta(no_oferta), 1700000005)
    assert dados["isVideo"] and dados["isGroup"]
    assert dados["groupJid"] == "123-456@g.us"
    assert dados["offerReceivedWhileOffline"]


def test_quem_ligou_prefere_o_numero_ao_lid():
    lid = jid("999", "lid")
    fone = jid("5511999")
    assert chamadas.quem_ligou(meta(criador=lid, alternativo=fone)) == "5511999@c.us"
    assert chamadas.quem_ligou(meta(criador=lid)) == "999@lid"
    assert chamadas.quem_ligou(meta(criador=fone, alternativo=jid("888", "lid"))) == "5511999@c.us"


def test_o_motivo_do_fim_vira_event_e_state():
    assert chamadas.estado_do_fim("timeout") == ("timeout", "NOT_ANSWERED")
    assert chamadas.estado_do_fim("Rejected") == ("ended", "REJECTED")
    assert chamadas.estado_do_fim("accepted_elsewhere") == ("ended", "HANDLED_REMOTELY")
    assert chamadas.estado_do_fim("") == ("ended", "ENDED")


def test_fim_leva_o_id_da_chamada_e_um_estado_fora_do_toque():
    fim = N(basicCallMeta=meta("C9"), reason="timeout", data=no("terminate"))
    dados = chamadas.evento_de_fim(fim, 1700000010)
    assert dados["id"] == "C9" and dados["state"] == "NOT_ANSWERED"
    assert dados["state"] not in {"", "3", "8", "OFFER", "INCOMING_RING", "RINGING"}


def test_pendentes_recusa_o_pedido_ou_todas():
    p = chamadas.Pendentes()
    p.lembrar("A", "criador-a", 100)
    p.lembrar("B", "criador-b", 101)
    assert p.a_recusar({"callId": "B"}) == ["B"]
    assert p.a_recusar({"callId": "Z"}) == []
    assert p.a_recusar({}) == ["A", "B"]
    assert p.a_recusar(None) == ["A", "B"]
    assert p.criador_de("A") == "criador-a"
    p.esquecer("A")
    assert p.criador_de("A") is None and p.a_recusar({}) == ["B"]


def test_pendentes_esquece_toque_antigo():
    p = chamadas.Pendentes()
    p.lembrar("velha", "x", 0)
    p.lembrar("nova", "y", chamadas.VALIDADE_DO_TOQUE + 1)
    assert p.a_recusar({}) == ["nova"]


def test_resposta_da_recusa_tem_o_que_o_cliente_le():
    resposta = chamadas.resposta_da_recusa(["A"])
    assert resposta["via"] == "neonize" and resposta["call"]["state"] == "REJECTED"


# ----------------------------------------------------------- as rotas, sem neonize
class ClienteFalso:
    def __init__(self, falha=None):
        self.recusadas = []
        self.falha = falha

    def reject_call(self, de, id_chamada):
        if self.falha:
            raise self.falha
        self.recusadas.append((de, id_chamada))


def rodar(s, requisicoes):
    """Sobe um servidor aiohttp so com as rotas de chamada e faz os pedidos."""
    async def principal():
        app = web.Application()
        chamadas.registrar(
            app.router, sessao=lambda req: s,
            ok=lambda **kw: web.json_response(kw))
        async with TestClient(TestServer(app)) as http:
            saidas = []
            for metodo, caminho, corpo in requisicoes:
                resp = await http.request(metodo, "/api/tok" + caminho, json=corpo)
                saidas.append((resp.status, await resp.json()))
            return saidas
    return asyncio.run(principal())


def sessao_com_toque(falha=None):
    s = N(cliente=ClienteFalso(falha), status="CONNECTED",
          chamadas_tocando=chamadas.Pendentes())
    s.chamadas_tocando.lembrar("C1", "criador-1", 100)
    return s


def test_recusar_chama_o_reject_call_com_o_criador_da_oferta():
    s = sessao_com_toque()
    (status, corpo), = rodar(s, [("POST", "/call/reject", {"callId": "C1"})])
    assert status == 200 and corpo["status"] == "success"
    assert corpo["response"]["call"]["state"] == "REJECTED"
    assert s.cliente.recusadas == [("criador-1", "C1")]
    assert s.chamadas_tocando.criador_de("C1") is None


def test_recusar_com_corpo_vazio_recusa_a_que_toca_e_reject_call_e_o_mesmo():
    s = sessao_com_toque()
    (status, _), = rodar(s, [("POST", "/reject-call", {})])
    assert status == 200 and s.cliente.recusadas == [("criador-1", "C1")]


def test_recusar_chamada_desconhecida_e_404_e_desconectado_e_400():
    s = sessao_com_toque()
    (status, corpo), = rodar(s, [("POST", "/call/reject", {"callId": "nao"})])
    assert status == 404 and corpo["status"] == "error" and not s.cliente.recusadas
    s.status = "CLOSED"
    (status, _), = rodar(s, [("POST", "/call/reject", {})])
    assert status == 400


def test_erro_do_whatsmeow_na_recusa_e_500_e_a_chamada_continua_pendente():
    s = sessao_com_toque(falha=RuntimeError("boom"))
    (status, corpo), = rodar(s, [("POST", "/call/reject", {"callId": "C1"})])
    assert status == 500 and corpo["status"] == "error"
    assert s.chamadas_tocando.criador_de("C1") == "criador-1"


def test_rotas_impossiveis_respondem_501_no_formato_de_erro():
    s = sessao_com_toque()
    saidas = rodar(s, [("POST", "/" + rota, {}) for rota in chamadas.ROTAS_IMPOSSIVEIS])
    assert len(saidas) == 9
    for status, corpo in saidas:
        assert status == 501 and corpo["status"] == "error" and corpo["message"]
    # uma chamada que ainda toca nao e mexida por elas
    assert s.chamadas_tocando.criador_de("C1") == "criador-1"
    # o texto nao se confunde com falha de inicializacao do VoIP do Chrome
    assert "voip" not in chamadas.MOTIVO_IMPOSSIVEL.lower()
