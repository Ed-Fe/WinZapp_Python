"""/message-comments/:messageId no servidor de verdade (criar_app), com um cliente
neonize de mentira. Sem WhatsApp; o envio nunca foi provado contra uma conta real."""
import os
from types import SimpleNamespace as N

import pytest
from aiohttp.test_utils import TestClient, TestServer

import autenticacao as aut
import comentarios as com
import server
from test_comentarios import (AUTOR, EU, EU_LID, GRUPO, SEGREDO, banco_do_whatsmeow,
                              comentario_cifrado, evento_de)

SERIAL = f"false_{GRUPO}_P_{AUTOR}"


class ClienteFalso:
    def __init__(self, aviso=True):
        self.aviso = aviso
        self.consultas = 0
        self.enviadas = []
        self.falha_ao_enviar = None
        self.falha_na_consulta = None

    def get_group_info(self, jid):
        self.consultas += 1
        if self.falha_na_consulta:
            raise self.falha_na_consulta
        return N(GroupIsDefaultSub=N(IsDefaultSubGroup=self.aviso))

    def send_message(self, jid, mensagem):
        if self.falha_ao_enviar:
            raise self.falha_ao_enviar
        self.enviadas.append((f"{jid.User}@{jid.Server}", mensagem))
        return N(ID="NOVO1")


@pytest.fixture
def sessoes(monkeypatch):
    criadas = []

    class Registrada(server.Sessao):
        def __init__(self, *args, **kw):
            super().__init__(*args, **kw)
            criadas.append(self)
    monkeypatch.setattr(server, "Sessao", Registrada)
    return criadas


async def _ponte(pasta, sessoes, conectada=True, aviso=True, segredos=True):
    http = TestClient(TestServer(server.criar_app(str(pasta))))
    await http.start_server()
    segredo = (pasta / aut.ARQUIVO_DO_SEGREDO).read_text(encoding="utf-8")
    token = f"conta:{aut.token_da_sessao(segredo, 'conta')}"
    assert (await http.get(f"/api/{token}/status-session")).status == 200
    s = sessoes[0]
    falso = ClienteFalso(aviso)
    s.cliente = falso
    s.status = "CONNECTED" if conectada else "CLOSED"
    s.eu, s.lid = EU, EU_LID
    banco_do_whatsmeow(os.path.join(s.pasta, "sessao.db"),
                       [(GRUPO, AUTOR, "P", SEGREDO)] if segredos else [], mapa=[])
    return http, f"/api/{token}/message-comments/", s, falso


@pytest.mark.asyncio
async def test_lista_vazia_e_depois_os_comentarios_recebidos(tmp_path, sessoes):
    http, api, s, falso = await _ponte(tmp_path, sessoes)
    try:
        resposta = await http.get(api + SERIAL)
        assert resposta.status == 200
        assert await resposta.json() == {"status": "success", "response": []}
        o, e = evento_de(comentario_cifrado("ola", quem="444555666@lid"), id_msg="C1",
                       quem="444555666@lid")
        assert com.receber(s, GRUPO, o, e)
        corpo = await (await http.get(api + SERIAL)).json()
        assert corpo["response"] == [{
            "id": "C1", "parentMsgId": SERIAL, "chatId": GRUPO, "author": "444555666@lid",
            "authorName": "Ana", "fromMe": False, "timestamp": 1700000000,
            "type": "comment", "body": "ola"}]
        # o que core/community_comments.py::parse_comments confere
        linha = corpo["response"][0]
        assert linha["parentMsgId"] == SERIAL and linha["chatId"] == SERIAL.split("_", 2)[1]
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_grupo_comum_e_conversa_privada_respondem_400(tmp_path, sessoes):
    http, api, s, falso = await _ponte(tmp_path, sessoes, aviso=False)
    try:
        r = await http.get(api + SERIAL)
        assert r.status == 400 and (await r.json())["code"] == "not_community_announcement"
        r = await http.get(api + "false_5511999990000@c.us_ID1")
        assert r.status == 400 and (await r.json())["code"] == "not_community_announcement"
        r = await http.post(api + SERIAL, json={"text": "oi"})
        assert r.status == 400 and (await r.json())["code"] == "not_community_announcement"
        assert falso.enviadas == []
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_a_resposta_do_whatsapp_sobre_o_grupo_fica_guardada(tmp_path, sessoes):
    http, api, s, falso = await _ponte(tmp_path, sessoes)
    try:
        await http.get(api + SERIAL)
        await http.get(api + SERIAL)
        assert falso.consultas == 1
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_sem_conexao_ou_sem_saber_do_grupo_e_503_e_id_ruim_e_400(tmp_path, sessoes):
    http, api, s, falso = await _ponte(tmp_path, sessoes, conectada=False)
    try:
        r = await http.get(api + SERIAL)
        assert r.status == 503 and (await r.json())["code"] == "comments_unavailable"
        s.status = "CONNECTED"
        falso.falha_na_consulta = RuntimeError("timeout")
        r = await http.get(api + SERIAL)
        assert r.status == 503 and (await r.json())["code"] == "comments_unavailable"
        for ruim in ("x", "true_abc", f"talvez_{GRUPO}_P"):
            r = await http.get(api + ruim)
            assert r.status == 400 and (await r.json())["code"] == "invalid_comment_request"
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_comentar_cifra_envia_guarda_e_avisa(tmp_path, sessoes):
    http, api, s, falso = await _ponte(tmp_path, sessoes)
    try:
        r = await http.post(api + SERIAL, json={"text": "meu comentario"})
        assert r.status == 201
        assert await r.json() == {"status": "success", "response": {"messageSendResult": "OK"}}
        (jid, mensagem), = falso.enviadas
        assert jid == GRUPO
        enc = mensagem.encCommentMessage
        chave = enc.targetMessageKey
        assert (chave.remoteJID, chave.participant, chave.fromMe, chave.ID) == (
            GRUPO, AUTOR, False, "P")
        # quem le com o mesmo segredo, como a conta (LID), obtem o texto
        aberto = com.decifrar_comentario(SEGREDO, "P", [AUTOR], [EU_LID],
                                         bytes(enc.encIV), bytes(enc.encPayload))
        from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message
        assert Message.FromString(aberto).conversation == "meu comentario"
        # o servidor nao devolve o proprio comentario a este aparelho: fica guardado
        corpo = await (await http.get(api + SERIAL)).json()
        linha, = corpo["response"]
        assert (linha["id"], linha["body"], linha["fromMe"]) == ("NOVO1", "meu comentario", True)
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_comentar_sem_segredo_nao_envia_nada(tmp_path, sessoes):
    http, api, s, falso = await _ponte(tmp_path, sessoes, segredos=False)
    try:
        r = await http.post(api + SERIAL, json={"text": "oi"})
        assert r.status == 503
        assert (await r.json())["code"] == "comment_secret_unavailable"
        assert falso.enviadas == []
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_falha_no_envio_e_nao_confirmado_e_nao_guarda(tmp_path, sessoes):
    http, api, s, falso = await _ponte(tmp_path, sessoes)
    try:
        falso.falha_ao_enviar = RuntimeError("timeout")
        r = await http.post(api + SERIAL, json={"text": "oi"})
        assert r.status == 502
        assert (await r.json()) == {"status": "error", "code": "comment_unconfirmed",
                                    "retrySafe": False}
        assert s.bd.comentarios_de(GRUPO, "P") == []
    finally:
        await http.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("corpo", [{}, {"text": "  "}, {"text": 3}, None])
async def test_comentario_sem_texto_e_400(tmp_path, sessoes, corpo):
    http, api, s, falso = await _ponte(tmp_path, sessoes)
    try:
        r = await http.post(api + SERIAL, json=corpo)
        assert r.status == 400 and (await r.json())["code"] == "invalid_comment_request"
        assert falso.enviadas == []
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_comentar_em_mensagem_minha_usa_o_segredo_e_o_remetente_guardados(tmp_path, sessoes):
    http, api, s, falso = await _ponte(tmp_path, sessoes)
    try:
        os.remove(os.path.join(s.pasta, "sessao.db"))
        banco_do_whatsmeow(os.path.join(s.pasta, "sessao.db"),
                           [(GRUPO, EU_LID, "MEU", SEGREDO)], mapa=[])
        r = await http.post(api + f"true_{GRUPO}_MEU", json={"text": "e eu"})
        assert r.status == 201
        enc = falso.enviadas[0][1].encCommentMessage
        assert enc.targetMessageKey.fromMe is True
        assert enc.targetMessageKey.participant == EU_LID
        assert com.decifrar_comentario(SEGREDO, "MEU", [EU_LID], [EU_LID],
                                       bytes(enc.encIV), bytes(enc.encPayload))
    finally:
        await http.close()
