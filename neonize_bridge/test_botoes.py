"""Botoes de mensagens de empresa: do proto para os campos que o WinZapp le, o
plano de resposta, o proto da resposta e a rota reply-button."""
import asyncio
from types import SimpleNamespace as N

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import ContextInfo, Message

import botoes
import conversas_interativas as ci
from store import Armazem


def com_botoes():
    m = Message()
    m.buttonsMessage.contentText = "Escolha"
    for i, (id_, texto) in enumerate((("sim", "Sim"), ("nao", "Nao"))):
        b = m.buttonsMessage.buttons.add()
        b.buttonID, b.type = id_, 1
        b.buttonText.displayText = texto
    nativo = m.buttonsMessage.buttons.add()      # NATIVE_FLOW nao e resposta rapida
    nativo.type = 2
    return m


def com_lista():
    m = Message()
    m.listMessage.title, m.listMessage.description = "Menu", "Pedidos"
    m.listMessage.buttonText = "Ver"
    secao = m.listMessage.sections.add()
    secao.title = "A"
    for rid, titulo in (("r1", "Um"), ("r2", "Dois")):
        linha = secao.rows.add()
        linha.rowID, linha.title, linha.description = rid, titulo, "d"
    outra = m.listMessage.sections.add()
    linha = outra.rows.add()
    linha.rowID, linha.title = "r3", "Tres"
    return m


def com_modelo():
    m = Message()
    t = m.templateMessage.hydratedTemplate
    t.hydratedContentText = "Oi"
    q = t.hydratedButtons.add()
    q.quickReplyButton.displayText, q.quickReplyButton.ID = "Ok", "ok"
    u = t.hydratedButtons.add()
    u.urlButton.displayText, u.urlButton.URL = "Site", "https://x.test"
    t.hydratedButtons.add()
    return m


def com_fluxo_nativo():
    m = Message()
    m.interactiveMessage.body.text = "Corpo"
    b = m.interactiveMessage.nativeFlowMessage.buttons.add()
    b.name, b.buttonParamsJSON = "quick_reply", '{"display_text":"Oi","id":"x"}'
    return m


def test_mensagem_sem_botoes_nao_ganha_campos():
    assert botoes.campos_de_botoes(Message(conversation="oi")) == {}
    assert botoes.campos_de_botoes(Message()) == {}


def test_botoes_de_resposta():
    campos = botoes.campos_de_botoes(com_botoes())
    assert campos["body"] == "Escolha"
    assert campos["dynamicReplyButtons"][:2] == [
        {"buttonId": "sim", "buttonText": {"displayText": "Sim"}, "type": 1},
        {"buttonId": "nao", "buttonText": {"displayText": "Nao"}, "type": 1}]
    assert len(campos["dynamicReplyButtons"]) == 2      # o NATIVE_FLOW nao entra


def test_lista_vira_secoes_e_linhas():
    campos = botoes.campos_de_botoes(com_lista())
    assert campos["type"] == "list" and campos["body"] == "Pedidos"
    secoes = campos["list"]["sections"]
    assert [r["rowId"] for s in secoes for r in s["rows"]] == ["r1", "r2", "r3"]
    assert campos["list"]["buttonText"] == "Ver"


def test_modelo_guarda_a_posicao_de_cada_botao():
    campos = botoes.campos_de_botoes(com_modelo())
    assert campos["body"] == "Oi"
    assert campos["hydratedButtons"][0] == {
        "index": 0, "quickReplyButton": {"displayText": "Ok", "id": "ok"}}
    assert campos["hydratedButtons"][1]["urlButton"]["url"] == "https://x.test"
    assert campos["hydratedButtons"][2] == {}


def test_fluxo_nativo():
    campos = botoes.campos_de_botoes(com_fluxo_nativo())
    assert campos["body"] == "Corpo"
    assert campos["interactivePayload"]["buttons"] == [
        {"name": "quick_reply", "buttonParamsJson": '{"display_text":"Oi","id":"x"}'}]


def guardada(m):
    return botoes.campos_de_botoes(m)


def test_plano_de_cada_origem():
    assert botoes.plano_da_resposta(guardada(com_botoes()), "dynamic", 1) == {
        "tipo": "botao", "id": "nao", "texto": "Nao"}
    assert botoes.plano_da_resposta(guardada(com_modelo()), "template", 0) == {
        "tipo": "modelo", "id": "ok", "texto": "Ok", "indice": 0}
    # a posicao da linha conta as secoes em sequencia, como o cliente
    assert botoes.plano_da_resposta(guardada(com_lista()), "list", 2) == {
        "tipo": "lista", "id": "r3", "titulo": "Tres", "descricao": ""}


def test_plano_recusa_botao_que_nao_existe_ou_nao_e_resposta():
    def recusa(wpp, source, indice):
        with pytest.raises(botoes.BotaoRecusado) as e:
            botoes.plano_da_resposta(wpp, source, indice)
        return e.value.codigo, e.value.status
    assert recusa(guardada(com_botoes()), "dynamic", 5) == ("button_not_found", 404)
    assert recusa({}, "dynamic", 0) == ("button_not_found", 404)
    assert recusa(guardada(com_modelo()), "template", 1) == ("button_not_found", 404)   # link
    assert recusa(guardada(com_modelo()), "template", 2) == ("button_not_found", 404)   # vazio
    assert recusa(guardada(com_lista()), "list", 3) == ("button_not_found", 404)
    assert recusa({}, "outra", 0) == ("invalid_request", 400)


def test_resposta_rapida_de_fluxo_nativo_nao_tem_formato_confirmado():
    wpp = guardada(com_fluxo_nativo())
    with pytest.raises(botoes.BotaoRecusado) as e:
        botoes.plano_da_resposta(wpp, "native", 0)
    assert (e.value.codigo, e.value.status) == ("reply_button_unavailable", 501)


def test_protos_da_resposta():
    ctx = ContextInfo(stanzaID="ID1")
    r = botoes.montar_resposta({"tipo": "botao", "id": "sim", "texto": "Sim"}, ctx)
    assert r.buttonsResponseMessage.selectedButtonID == "sim"
    assert r.buttonsResponseMessage.selectedDisplayText == "Sim"
    assert r.buttonsResponseMessage.contextInfo.stanzaID == "ID1"
    r = botoes.montar_resposta({"tipo": "modelo", "id": "ok", "texto": "Ok", "indice": 3}, ctx)
    assert (r.templateButtonReplyMessage.selectedID,
            r.templateButtonReplyMessage.selectedIndex) == ("ok", 3)
    r = botoes.montar_resposta(
        {"tipo": "lista", "id": "r1", "titulo": "Um", "descricao": "d"}, ctx)
    assert r.listResponseMessage.singleSelectReply.selectedRowID == "r1"
    assert r.listResponseMessage.title == "Um" and r.listResponseMessage.description == "d"


def test_pedido_valido():
    serial = "false_5511999990000@c.us_ABCDEF1234"
    assert ci.pedido_valido(serial, {"source": "list", "index": 0}) == ("list", 0)
    ruins = [
        ("false_1203@g.us_ABCDEF1234", {"source": "list", "index": 0}),      # grupo
        (serial, {"source": "x", "index": 0}),
        (serial, {"source": "list", "index": -1}),
        (serial, {"source": "list", "index": 12}),
        (serial, {"source": "list", "index": True}),
        (serial, {"source": "list", "index": "1"}),
        (serial, None),
    ]
    for s, corpo in ruins:
        with pytest.raises(botoes.BotaoRecusado) as e:
            ci.pedido_valido(s, corpo)
        assert e.value.status == 400


SERIAL = "false_5511999990000@c.us_ABCDEF1234"


def pedir(tmp_path, corpo, conectada=True, guardar=True, falha=False, serial=SERIAL):
    bd = Armazem(str(tmp_path / "x.db"))
    if guardar:
        original = {"id": {"id": "ABCDEF1234", "_serialized": SERIAL}, "t": 5,
                    "fromMe": False, "type": "chat", **guardada(com_botoes())}
        bd.mensagem("5511999990000@s.whatsapp.net", original)
    enviadas = []

    def send_message(destino, mensagem):
        if falha:
            raise RuntimeError("recusado")
        enviadas.append((destino, mensagem))
    s = N(bd=bd, cliente=N(send_message=send_message) if conectada else None,
          status="CONNECTED" if conectada else "CLOSED",
          partes_do_id=staticmethod(lambda ser: (
              ser.startswith("true_"), "5511999990000@s.whatsapp.net", ser.split("_")[2], "")))

    async def rodar():
        app = web.Application()
        ci.registrar(app.router, sessao=lambda req: s, ok=lambda **kw: web.json_response(kw),
                     destino_de=lambda texto: "JID:" + texto,
                     contexto_citado=lambda sessao, citado: ContextInfo(stanzaID="ABCDEF1234"))
        async with TestClient(TestServer(app)) as cliente:
            resp = await cliente.post("/api/t/reply-button/" + serial, json=corpo)
            return resp.status, await resp.json()
    status, resposta = asyncio.run(rodar())
    return status, resposta, enviadas


def test_rota_envia_a_resposta_uma_vez(tmp_path):
    status, corpo, enviadas = pedir(tmp_path, {"source": "dynamic", "index": 0})
    assert status == 200 and corpo == {"status": "success", "response": {"sent": True}}
    assert len(enviadas) == 1
    destino, mensagem = enviadas[0]
    assert destino == "JID:5511999990000@s.whatsapp.net"
    assert mensagem.buttonsResponseMessage.selectedButtonID == "sim"


def test_rota_responde_com_o_codigo_que_o_cliente_le(tmp_path):
    cenarios = [
        ({"source": "dynamic", "index": 9}, {}, 404, "button_not_found"),
        ({"source": "dynamic"}, {}, 400, "invalid_request"),
        ({"source": "native", "index": 0}, {}, 404, "button_not_found"),
        ({"source": "dynamic", "index": 0}, {"guardar": False}, 404, "message_not_found"),
        ({"source": "dynamic", "index": 0}, {"conectada": False}, 503, "reply_button_unavailable"),
        ({"source": "dynamic", "index": 0}, {"falha": True}, 503, "reply_button_unavailable"),
    ]
    for i, (corpo, extra, status, codigo) in enumerate(cenarios):
        pasta = tmp_path / str(i)       # um armazem novo por cenario
        pasta.mkdir()
        obtido, resposta, enviadas = pedir(pasta, corpo, **extra)
        assert (obtido, resposta) == (status, {"status": "error", "code": codigo}), corpo
        assert not enviadas
