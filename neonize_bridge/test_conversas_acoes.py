"""Rotas de acao sobre conversas: funcoes puras, o armazem (limpar, apagar,
recibos) e as rotas que nao precisam do neonize."""
import asyncio
from types import SimpleNamespace as N

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import conversas_acoes as ca
from store import Armazem


def test_manter_estrelas_por_padrao():
    assert ca.manter_estrelas({}) is True
    assert ca.manter_estrelas({"keepStarred": True}) is True
    assert ca.manter_estrelas({"keepStarred": False}) is False
    assert ca.manter_estrelas({"keepStarred": "false"}) is False


def test_indice_de_limpar():
    assert ca.indice_de_limpar(True) == ("0", "1")
    assert ca.indice_de_limpar(False) == ("1", "1")


def test_telefones_aceita_lista_ou_texto():
    assert ca.telefones({"phone": ["1@c.us", "2@c.us"]}) == ["1@c.us", "2@c.us"]
    assert ca.telefones({"phone": "1@c.us"}) == ["1@c.us"]
    for corpo in ({}, {"phone": []}, {"phone": [""]}):
        with pytest.raises(ValueError):
            ca.telefones(corpo)


def test_estado_de_digitacao():
    assert ca.estado_de_digitacao(0) == ("CHAT_PRESENCE_COMPOSING", "CHAT_PRESENCE_MEDIA_TEXT")
    assert ca.estado_de_digitacao("1") == ("CHAT_PRESENCE_COMPOSING", "CHAT_PRESENCE_MEDIA_AUDIO")
    assert ca.estado_de_digitacao(2)[0] == "CHAT_PRESENCE_PAUSED"
    for ruim in (3, None, "x"):
        with pytest.raises(ValueError):
            ca.estado_de_digitacao(ruim)


def test_participantes_do_recibo_uma_linha_por_pessoa():
    linhas = [("a@c.us", "delivered", 10), ("a@c.us", "read", 20),
              ("b@c.us", "delivered", 12), ("c@c.us", "played", 30)]
    saida = {p["id"]: p for p in ca.participantes_do_recibo(linhas)}
    assert saida["a@c.us"] == {"id": "a@c.us", "deliveredAt": 10, "readAt": 20}
    assert saida["b@c.us"] == {"id": "b@c.us", "deliveredAt": 12}
    # quem ouviu tambem recebeu: sem recibo de entrega, a entrega leva a mesma hora
    assert saida["c@c.us"] == {"id": "c@c.us", "playedAt": 30, "deliveredAt": 30}


def test_participantes_do_recibo_ignora_etapa_desconhecida():
    assert ca.participantes_do_recibo([("a@c.us", "sent", 5)]) == []


def msg(mid, estrela=False, t=1):
    return {"id": {"id": mid, "fromMe": False}, "fromMe": False, "t": t, "star": estrela}


def test_limpar_conversa_mantem_as_estreladas(tmp_path):
    bd = Armazem(str(tmp_path / "x.db"))
    bd.conversa("a@s.whatsapp.net", unread=3)
    bd.mensagem("a@s.whatsapp.net", msg("1", estrela=True))
    bd.mensagem("a@s.whatsapp.net", msg("2"))
    bd.mensagem("b@s.whatsapp.net", msg("3"))
    assert bd.limpar_conversa("a@s.whatsapp.net", manter_estrelas=True) == 1
    assert [m["id"]["id"] for m in bd.mensagens("a@s.whatsapp.net")] == ["1"]
    assert [m["id"]["id"] for m in bd.mensagens("b@s.whatsapp.net")] == ["3"]
    assert bd.nao_lidas("a@s.whatsapp.net") == 0


def test_limpar_conversa_sem_manter_apaga_tudo_e_solta_as_fixadas(tmp_path):
    bd = Armazem(str(tmp_path / "x.db"))
    bd.mensagem("a@s.whatsapp.net", msg("1", estrela=True))
    bd.fixar_mensagem("1", "a@s.whatsapp.net", True, ts=1)
    assert bd.limpar_conversa("a@s.whatsapp.net", manter_estrelas=False) == 1
    assert bd.mensagens("a@s.whatsapp.net") == []
    assert bd.fixadas_de(["a@s.whatsapp.net"], 2) == []


def test_apagar_conversa_tira_da_lista(tmp_path):
    bd = Armazem(str(tmp_path / "x.db"))
    bd.conversa("a@s.whatsapp.net", name="Ana")
    bd.mensagem("a@s.whatsapp.net", msg("1", estrela=True))
    bd.apagar_conversa("a@s.whatsapp.net")
    assert not bd.existe_conversa("a@s.whatsapp.net")
    assert bd.mensagens("a@s.whatsapp.net") == []


def test_recibo_guarda_a_primeira_hora_de_cada_etapa(tmp_path):
    bd = Armazem(str(tmp_path / "x.db"))
    bd.recibo("m1", "a@c.us", "delivered", 10)
    bd.recibo("m1", "a@c.us", "delivered", 99)
    bd.recibo("m1", "a@c.us", "read", 20)
    assert bd.recibos_de("m1") == [("a@c.us", "delivered", 10), ("a@c.us", "read", 20)]
    assert bd.recibos_de("outra") == []


def pedir(tmp_path, caminho, corpo, preparar=lambda bd: None, conectada=True):
    bd = Armazem(str(tmp_path / "x.db"))
    preparar(bd)
    s = N(bd=bd, cliente=object() if conectada else None,
          status="CONNECTED" if conectada else "CLOSED",
          partes_do_id=staticmethod(lambda serial: (
              serial.startswith("true_"), "", serial.split("_")[2] if serial.count("_") >= 2 else serial, "")))

    async def em_thread(funcao, *args):
        return funcao(*args)

    async def rodar():
        app = web.Application()
        ca.registrar(
            app.router, sessao=lambda req: s, ok=lambda **kw: web.json_response(kw),
            erro=lambda m, status=500: web.json_response(
                {"status": "error", "message": str(m)}, status=status),
            em_thread=em_thread)
        async with TestClient(TestServer(app)) as cliente:
            resp = await cliente.post(caminho, json=corpo)
            return resp.status, await resp.json()
    return asyncio.run(rodar())


def test_message_ack_devolve_os_participantes(tmp_path):
    def preparar(bd):
        bd.recibo("M1", "a@c.us", "delivered", 10)
        bd.recibo("M1", "a@c.us", "read", 20)
    status, corpo = pedir(tmp_path, "/api/t/message-ack",
                          {"messageId": "true_55@c.us_M1"}, preparar)
    assert status == 200
    assert corpo["response"] == {
        "participants": [{"id": "a@c.us", "deliveredAt": 10, "readAt": 20}],
        "deliveryRemaining": 0}


def test_message_ack_sem_recibo_ou_de_mensagem_recebida_e_404(tmp_path):
    status, _ = pedir(tmp_path, "/api/t/message-ack", {"messageId": "true_55@c.us_M1"})
    assert status == 404

    def preparar(bd):
        bd.recibo("M2", "a@c.us", "read", 20)
    status, _ = pedir(tmp_path, "/api/t/message-ack", {"messageId": "false_55@c.us_M2"}, preparar)
    assert status == 404


def test_clear_chat_valida_o_corpo_e_a_conexao(tmp_path):
    status, _ = pedir(tmp_path, "/api/t/clear-chat", {"isGroup": False})
    assert status == 400
    status, corpo = pedir(tmp_path, "/api/t/clear-chat", {"phone": ["55@c.us"]}, conectada=False)
    assert status == 400 and corpo["message"] == "Disconnected"


def test_clear_chat_responde_no_formato_que_o_cliente_confere(tmp_path, monkeypatch):
    monkeypatch.setattr(ca, "limpar_no_celular", lambda s, jid, manter: True)
    status, corpo = pedir(tmp_path, "/api/t/clear-chat",
                          {"phone": ["55@c.us"], "isGroup": False, "keepStarred": False})
    assert status == 200
    assert corpo["response"] == {"data": {"55@c.us": True}, "keepStarred": False}


def test_clear_chat_recusado_pelo_whatsapp_e_500(tmp_path, monkeypatch):
    monkeypatch.setattr(ca, "limpar_no_celular", lambda s, jid, manter: False)
    status, _ = pedir(tmp_path, "/api/t/clear-chat", {"phone": ["55@c.us"]})
    assert status == 500


def test_delete_chat_e_as_rotas_em_massa(tmp_path, monkeypatch):
    apagadas = []
    monkeypatch.setattr(ca, "apagar_no_celular", lambda s, jid: apagadas.append(jid) or True)
    status, corpo = pedir(tmp_path, "/api/t/delete-chat", {"phone": ["55@c.us"]})
    assert status == 200 and corpo["response"]["data"] == {"55@c.us": True}

    def preparar(bd):
        bd.conversa("a@s.whatsapp.net")
        bd.conversa("b@s.whatsapp.net")
        bd.conversa("status@broadcast")
    apagadas.clear()
    status, corpo = pedir(tmp_path, "/api/t/delete-all-chats", {}, preparar)
    assert corpo["response"] == {"total": 2, "done": 2}
    assert sorted(apagadas) == ["a@s.whatsapp.net", "b@s.whatsapp.net"]


def test_chat_state_recusa_valor_desconhecido(tmp_path):
    status, _ = pedir(tmp_path, "/api/t/chat-state", {"phone": "55", "chatstate": 7})
    assert status == 400
