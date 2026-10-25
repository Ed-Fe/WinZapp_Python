"""Rotas de conversas e mensagens do servidor de verdade (criar_app) contra um
cliente neonize falso: o formato das respostas que o WinZapp le, sem WhatsApp.

Nada aqui fala com uma conta real: o falso so guarda o que a ponte lhe pediu."""
import json
from types import SimpleNamespace as N

import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer
from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message

import autenticacao as aut
import server

PN = "5511999990000@s.whatsapp.net"
LID = "111222333@lid"
EU = "5500000000@s.whatsapp.net"


class _Jid:
    def __init__(self, usuario, servidor):
        self.User, self.Server = usuario, servidor


class Falso:
    """O que a ponte pede ao neonize, registrado."""

    def __init__(self):
        self.enviadas, self.presencas, self.revogadas, self.editadas = [], [], [], []
        self.lidas, self.erro_no_envio = [], None

    def send_message(self, destino, mensagem):
        if self.erro_no_envio:
            raise RuntimeError(self.erro_no_envio)
        self.enviadas.append((f"{destino.User}@{destino.Server}", mensagem))
        if isinstance(mensagem, str):
            mensagem = Message(conversation=mensagem)
        return N(ID="NOVA1", Timestamp=1_700_000_000, Message=mensagem)

    def get_lid_from_pn(self, jid):
        return _Jid("111222333", "lid") if jid.User == "5511999990000" else _Jid("", "")

    def get_pn_from_lid(self, jid):
        return _Jid("5511999990000", "s.whatsapp.net") if jid.User == "111222333" else _Jid("", "")

    def send_chat_presence(self, jid, presenca, midia):
        self.presencas.append((f"{jid.User}@{jid.Server}", presenca.name, midia.name))

    def mark_read(self, *ids, chat, sender, receipt):
        if self.erro_na_leitura:
            raise RuntimeError("falhou")
        self.lidas.append((ids, f"{chat.User}@{chat.Server}"))

    erro_na_leitura = False

    def revoke_message(self, chat, sender, mid):
        self.revogadas.append((f"{chat.User}@{chat.Server}", f"{sender.User}@{sender.Server}", mid))

    def edit_message(self, chat, mid, nova):
        self.editadas.append((f"{chat.User}@{chat.Server}", mid))


@pytest.fixture
def sessoes(monkeypatch):
    criadas = []

    class Registrada(server.Sessao):
        def __init__(self, *args, **kw):
            super().__init__(*args, **kw)
            criadas.append(self)
    monkeypatch.setattr(server, "Sessao", Registrada)
    return criadas


class Ponte:
    def __init__(self, http, token, s, falso):
        self.http, self.token, self.s, self.falso = http, token, s, falso

    def url(self, rota):
        return f"/api/{self.token}/{rota}"

    async def post(self, rota, corpo=None, **kw):
        resposta = await self.http.post(self.url(rota), json=corpo or {}, **kw)
        return resposta.status, await resposta.json()

    async def get(self, rota):
        resposta = await self.http.get(self.url(rota))
        return resposta.status, await resposta.json()


async def _abrir(tmp_path, sessoes, conectada=True):
    http = TestClient(TestServer(server.criar_app(str(tmp_path))))
    await http.start_server()
    segredo = (tmp_path / aut.ARQUIVO_DO_SEGREDO).read_text(encoding="utf-8")
    token = f"conta1:{aut.token_da_sessao(segredo, 'conta1')}"
    await http.get(f"/api/{token}/status-session")          # cria a Sessao
    s = sessoes[0]
    falso = Falso()
    if conectada:
        s.cliente, s.status, s.eu = falso, "CONNECTED", EU
    return Ponte(http, token, s, falso)


def _msg(mid, ts, conversa, de_mim=False, **extra):
    w = {"id": {"id": mid, "fromMe": de_mim, "remote": conversa,
                "_serialized": f"{'true' if de_mim else 'false'}_{conversa}_{mid}"},
         "fromMe": de_mim, "t": ts, "timestamp": ts, "type": "chat", "body": mid,
         "from": "" if de_mim else conversa, "to": conversa}
    w.update(extra)
    return w


ROTAS_COM_CONEXAO = [
    ("send-message", {"phone": "5511@c.us", "message": "oi"}),
    ("send-reply", {"phone": "5511@c.us", "message": "oi", "messageId": "false_5511@c.us_X"}),
    ("send-mentioned", {"phone": "5511@c.us", "message": "oi"}),
    ("send-voice-base64", {"phone": "5511@c.us", "base64Ptt": "eA=="}),
    ("send-poll-message", {"phone": "5511@c.us", "name": "p", "choices": ["a", "b"]}),
    ("vote-poll", {"messageId": "true_5511@c.us_X", "localIds": [0]}),
    ("react-message", {"msgId": "true_5511@c.us_X", "reaction": "x"}),
    ("forward-messages", {"phone": "5511@c.us", "messageId": ["true_5511@c.us_X"]}),
    ("edit-message", {"id": "true_5511@c.us_X", "newText": "novo"}),
    ("delete-message", {"messageId": "true_5511@c.us_X"}),
    ("send-seen", {"phone": "5511@c.us"}),
    ("mark-played", {"messageId": "false_5511@c.us_X"}),
    ("star-message", {"messageId": "true_5511@c.us_X"}),
    ("pin-message", {"messageId": "true_5511@c.us_X"}),
    ("pin-chat", {"phone": "5511@c.us"}),
    ("archive-chat", {"phone": "5511@c.us"}),
    ("send-mute", {"phone": "5511@c.us", "time": 1, "type": "hours"}),
    ("typing", {"phone": "5511@c.us", "value": True}),
    ("recording", {"phone": "5511@c.us", "value": True}),
    ("chat-state", {"phone": "5511@c.us", "chatstate": 0}),
    ("clear-chat", {"phone": ["5511@c.us"]}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("rota,corpo", ROTAS_COM_CONEXAO)
async def test_sem_conexao_toda_rota_responde_o_404_disconnected_do_servidor_real(
        tmp_path, sessoes, rota, corpo):
    ponte = await _abrir(tmp_path, sessoes, conectada=False)
    try:
        status, resposta = await ponte.post(rota, corpo)
        assert status == 404, rota
        assert resposta["status"] == "Disconnected" and resposta["response"] is None
    finally:
        await ponte.http.close()


@pytest.mark.asyncio
async def test_send_file_sem_conexao_responde_disconnected(tmp_path, sessoes):
    ponte = await _abrir(tmp_path, sessoes, conectada=False)
    try:
        form = FormData()
        form.add_field("file", b"x", filename="a.bin", content_type="application/octet-stream")
        resposta = await ponte.http.post(ponte.url("send-file"), data=form)
        assert resposta.status == 404 and (await resposta.json())["status"] == "Disconnected"
    finally:
        await ponte.http.close()


# ------------------------------------------------------------ get-messages
@pytest.mark.asyncio
async def test_get_messages_acha_a_conversa_guardada_pelo_lid_quando_pedida_pelo_numero(
        tmp_path, sessoes):
    ponte = await _abrir(tmp_path, sessoes)
    try:
        ponte.s.bd.conversa(LID, name="Ana")
        ponte.s.bd.mensagem(LID, _msg("M1", 100, LID))
        ponte.s.bd.mensagem(LID, _msg("M2", 101, LID))
        status, corpo = await ponte.get("get-messages/5511999990000@c.us?count=10")
        assert status == 200
        assert [m["id"]["id"] for m in corpo["response"]] == ["M1", "M2"]
    finally:
        await ponte.http.close()


@pytest.mark.asyncio
async def test_get_messages_junta_as_linhas_dos_dois_enderecos(tmp_path, sessoes):
    ponte = await _abrir(tmp_path, sessoes)
    try:
        ponte.s.bd.conversa(LID)
        ponte.s.bd.conversa(PN)
        ponte.s.bd.mensagem(PN, _msg("M1", 100, PN))
        ponte.s.bd.mensagem(LID, _msg("M2", 101, LID))
        _, corpo = await ponte.get(f"get-messages/{LID}?count=10")
        assert [m["id"]["id"] for m in corpo["response"]] == ["M1", "M2"]
    finally:
        await ponte.http.close()


@pytest.mark.asyncio
async def test_get_messages_before_com_ancora_desconhecida_devolve_vazio(tmp_path, sessoes):
    ponte = await _abrir(tmp_path, sessoes)
    try:
        ponte.s.bd.conversa(LID)
        for n in range(1, 5):
            ponte.s.bd.mensagem(LID, _msg(f"M{n}", 100 + n, LID))
        _, corpo = await ponte.get(f"get-messages/{LID}?count=3&direction=before&id=false_{LID}_ZZZ")
        assert corpo["response"] == []
        _, corpo = await ponte.get(f"get-messages/{LID}?count=3&direction=before&id=false_{LID}_M3")
        assert [m["id"]["id"] for m in corpo["response"]] == ["M1", "M2"]
    finally:
        await ponte.http.close()


@pytest.mark.asyncio
async def test_get_messages_leva_ack_nas_minhas_e_star_sempre(tmp_path, sessoes):
    ponte = await _abrir(tmp_path, sessoes)
    try:
        ponte.s.bd.conversa(PN)
        ponte.s.bd.mensagem(PN, _msg("M1", 100, PN, de_mim=True))
        ponte.s.bd.mensagem(PN, _msg("M2", 101, PN, de_mim=True))
        ponte.s.bd.mensagem(PN, _msg("M3", 102, PN))
        ponte.s.bd.recibo("M1", PN, "read", 110)
        _, corpo = await ponte.get(f"get-messages/{PN}?count=10")
        por_id = {m["id"]["id"]: m for m in corpo["response"]}
        assert por_id["M1"]["ack"] == 3 and por_id["M2"]["ack"] == 1
        assert "ack" not in por_id["M3"]
        assert all(m["star"] is False for m in por_id.values())
    finally:
        await ponte.http.close()


@pytest.mark.asyncio
async def test_message_by_id_responde_data_como_o_servidor_real(tmp_path, sessoes):
    ponte = await _abrir(tmp_path, sessoes)
    try:
        ponte.s.bd.mensagem(PN, _msg("M1", 100, PN, de_mim=True))
        ponte.s.bd.recibo("M1", PN, "delivered", 110)
        status, corpo = await ponte.get(f"message-by-id/true_{PN}_M1")
        assert status == 200
        dados = corpo["response"]["data"]
        assert dados["id"]["id"] == "M1" and dados["ack"] == 2 and dados["star"] is False
    finally:
        await ponte.http.close()


# --------------------------------------------------------------- send-seen
@pytest.mark.asyncio
async def test_send_seen_de_conversa_desconhecida_confirma_false(tmp_path, sessoes):
    ponte = await _abrir(tmp_path, sessoes)
    try:
        status, corpo = await ponte.post("send-seen", {"phone": "9999@c.us"})
        assert status == 200 and corpo["response"]["data"] == [False]
        assert not ponte.s.bd.existe_conversa("9999@s.whatsapp.net")      # nada de linha nova
    finally:
        await ponte.http.close()


@pytest.mark.asyncio
async def test_send_seen_acha_a_conversa_pelo_outro_endereco_e_confirma(tmp_path, sessoes):
    ponte = await _abrir(tmp_path, sessoes)
    try:
        ponte.s.bd.conversa(LID, unread=1)
        ponte.s.bd.mensagem(LID, _msg("M1", 100, LID))
        ponte.s.estado_lida = lambda conversa, lida: True
        status, corpo = await ponte.post("send-seen", {"phone": "5511999990000@c.us"})
        assert corpo["response"]["data"] == [True]
        assert ponte.s.bd.nao_lidas(LID) == 0
        assert ponte.falso.lidas == [(("M1",), LID)]
    finally:
        await ponte.http.close()


@pytest.mark.asyncio
async def test_send_seen_com_recibo_que_nao_saiu_nao_confirma(tmp_path, sessoes):
    ponte = await _abrir(tmp_path, sessoes)
    try:
        ponte.s.bd.conversa(PN, unread=1)
        ponte.s.bd.mensagem(PN, _msg("M1", 100, PN))
        ponte.s.estado_lida = lambda conversa, lida: True
        ponte.falso.erro_na_leitura = True
        _, corpo = await ponte.post("send-seen", {"phone": PN})
        assert corpo["response"]["data"] == [False]
    finally:
        await ponte.http.close()


@pytest.mark.asyncio
async def test_send_seen_sem_nada_a_ler_confirma(tmp_path, sessoes):
    ponte = await _abrir(tmp_path, sessoes)
    try:
        ponte.s.bd.conversa(PN, unread=0)
        ponte.s.estado_lida = lambda conversa, lida: True
        _, corpo = await ponte.post("send-seen", {"phone": PN})
        assert corpo["response"]["data"] == [True]
    finally:
        await ponte.http.close()


# ------------------------------------------- arquivar / fixar / silenciar
@pytest.mark.asyncio
async def test_archive_chat_age_na_linha_real_sob_o_outro_endereco(tmp_path, sessoes):
    ponte = await _abrir(tmp_path, sessoes)
    try:
        ponte.s.bd.conversa(LID, name="Ana")
        ponte.s.enviar_estado = lambda *a, **k: True
        status, _ = await ponte.post("archive-chat", {"phone": "5511999990000@c.us", "value": True})
        assert status == 200
        linhas = {c["jid"]: c for c in ponte.s.bd.conversas()}
        assert set(linhas) == {LID}                       # nenhuma linha vazia nova
        assert linhas[LID]["archived"] == 1
    finally:
        await ponte.http.close()


@pytest.mark.asyncio
async def test_pin_e_mute_acompanham_as_duas_linhas_da_conversa(tmp_path, sessoes):
    ponte = await _abrir(tmp_path, sessoes)
    try:
        ponte.s.bd.conversa(LID)
        ponte.s.bd.conversa(PN)
        ponte.s.enviar_estado = lambda *a, **k: True
        assert (await ponte.post("pin-chat", {"phone": LID, "state": "true"}))[0] == 200
        assert (await ponte.post("send-mute", {"phone": LID, "time": 1, "type": "hours"}))[0] == 200
        linhas = {c["jid"]: c for c in ponte.s.bd.conversas()}
        assert linhas[LID]["pinned"] > 0 and linhas[PN]["pinned"] == linhas[LID]["pinned"]
        assert linhas[LID]["muted"] > 0 and linhas[PN]["muted"] == linhas[LID]["muted"]
    finally:
        await ponte.http.close()


@pytest.mark.asyncio
async def test_clear_chat_age_na_conversa_guardada_pelo_lid(tmp_path, sessoes, monkeypatch):
    import conversas_acoes
    ponte = await _abrir(tmp_path, sessoes)
    try:
        ponte.s.bd.conversa(LID)
        ponte.s.bd.mensagem(LID, _msg("M1", 100, LID))
        chamadas = []
        monkeypatch.setattr(conversas_acoes, "limpar_no_celular",
                            lambda s, jid, manter: chamadas.append(jid) or True)
        status, _ = await ponte.post("clear-chat", {"phone": ["5511999990000@c.us"]})
        assert status == 200 and chamadas == [LID]
    finally:
        await ponte.http.close()


# ------------------------------------------------------- typing / recording
@pytest.mark.asyncio
async def test_typing_e_recording_mandam_a_presenca_ao_whatsapp(tmp_path, sessoes):
    ponte = await _abrir(tmp_path, sessoes)
    try:
        assert (await ponte.post("typing", {"phone": PN, "value": True, "isGroup": False}))[0] == 200
        await ponte.post("typing", {"phone": PN, "value": False})
        await ponte.post("recording", {"phone": PN, "duration": 0, "value": True})
        await ponte.post("recording", {"phone": "1203@g.us", "value": False, "isGroup": True})
        assert ponte.falso.presencas == [
            (PN, "CHAT_PRESENCE_COMPOSING", "CHAT_PRESENCE_MEDIA_TEXT"),
            (PN, "CHAT_PRESENCE_PAUSED", "CHAT_PRESENCE_MEDIA_TEXT"),
            (PN, "CHAT_PRESENCE_COMPOSING", "CHAT_PRESENCE_MEDIA_AUDIO"),
            ("1203@g.us", "CHAT_PRESENCE_PAUSED", "CHAT_PRESENCE_MEDIA_TEXT")]
    finally:
        await ponte.http.close()


# ---------------------------------------------------- history-sync-status
@pytest.mark.asyncio
async def test_history_status_de_conta_nova_diz_que_o_historico_ainda_chega(tmp_path, sessoes):
    ponte = await _abrir(tmp_path, sessoes)
    try:
        _, corpo = await ponte.get("history-sync-status")
        r = corpo["response"]
        assert r["recentCompleted"] is False and r["initialSyncComplete"] is False
        ponte.s.bd.conversa(PN)                    # ja ha conversas guardadas
        _, corpo = await ponte.get("history-sync-status")
        assert corpo["response"]["recentCompleted"] is True
        assert corpo["response"]["storeCounts"]["chat"] == 1
    finally:
        await ponte.http.close()


@pytest.mark.asyncio
async def test_history_status_acompanha_o_progresso_dos_blocos(tmp_path, sessoes):
    import historico_progresso as hp
    ponte = await _abrir(tmp_path, sessoes)
    try:
        progresso = hp.progresso_de(ponte.s, 0)
        progresso.comecou(0)
        _, corpo = await ponte.get("history-sync-status")
        assert corpo["response"]["unprocessedChunks"] == 1
        assert corpo["response"]["recentCompleted"] is False
        progresso.terminou(0, percentual=100)
        _, corpo = await ponte.post("unblock-history-sync")
        assert corpo["response"]["unprocessedChunks"] == 0
        assert corpo["response"]["recentCompleted"] is True
    finally:
        await ponte.http.close()


# -------------------------------------------------------------- edit-message
@pytest.mark.asyncio
async def test_edit_message_recusado_antes_do_envio_diz_cannot_edit(tmp_path, sessoes):
    import time
    ponte = await _abrir(tmp_path, sessoes)
    try:
        agora = int(time.time())
        ponte.s.bd.mensagem(PN, _msg("VELHA", agora - 3600, PN, de_mim=True))
        ponte.s.bd.mensagem(PN, _msg("DELES", agora, PN))
        casos = [f"true_{PN}_VELHA", f"false_{PN}_DELES", f"true_{PN}_DESCONHECIDA"]
        for serial in casos:
            resposta = await ponte.http.post(
                ponte.url("edit-message"), json={"id": serial, "newText": "novo"})
            assert resposta.status == 400 and "Cannot edit this message" in await resposta.text()
        assert ponte.falso.editadas == []                 # nada chegou ao WhatsApp
    finally:
        await ponte.http.close()


@pytest.mark.asyncio
async def test_edit_message_dentro_da_janela_edita(tmp_path, sessoes):
    import time
    ponte = await _abrir(tmp_path, sessoes)
    try:
        ponte.s.bd.mensagem(PN, _msg("NOVA", int(time.time()) - 60, PN, de_mim=True))
        status, corpo = await ponte.post("edit-message", {"id": f"true_{PN}_NOVA", "newText": "x"})
        assert status == 200 and corpo["response"] == {"edited": True}
        assert ponte.falso.editadas == [(PN, "NOVA")]
    finally:
        await ponte.http.close()


# ------------------------------------------------------------ citacao ausente
@pytest.mark.asyncio
@pytest.mark.parametrize("rota", ["send-reply", "send-mentioned"])
async def test_citacao_desconhecida_e_a_recusa_500_que_o_cliente_entende(
        tmp_path, sessoes, rota):
    ponte = await _abrir(tmp_path, sessoes)
    try:
        status, corpo = await ponte.post(
            rota, {"phone": PN, "message": "oi", "messageId": f"false_{PN}_NAOEXISTE"})
        assert status == 500
        assert corpo == {"status": "error", "code": "quoted_message_not_found",
                         "stage": "quote_resolution", "sendAttempted": False}
        assert ponte.falso.enviadas == []
    finally:
        await ponte.http.close()


@pytest.mark.asyncio
async def test_send_file_com_citacao_desconhecida_nao_envia_sem_citar(tmp_path, sessoes):
    ponte = await _abrir(tmp_path, sessoes)
    try:
        form = FormData()
        form.add_field("phone", PN)
        form.add_field("quotedMessageId", f"false_{PN}_NAOEXISTE")
        form.add_field("file", b"x", filename="a.bin", content_type="application/octet-stream")
        resposta = await ponte.http.post(ponte.url("send-file"), data=form)
        corpo = await resposta.json()
        assert resposta.status == 500 and corpo["code"] == "quoted_message_not_found"
        assert corpo["sendAttempted"] is False and ponte.falso.enviadas == []
    finally:
        await ponte.http.close()


# ---------------------------------------------------------- link preview e erros
@pytest.mark.asyncio
async def test_send_message_leva_o_cartao_do_link_que_o_cliente_montou(tmp_path, sessoes):
    ponte = await _abrir(tmp_path, sessoes)
    try:
        previa = {"title": "Titulo", "description": "Descricao",
                  "canonicalUrl": "https://exemplo.org/a", "matchedText": "https://exemplo.org/a"}
        status, _ = await ponte.post("send-message", {
            "phone": [PN], "message": "veja https://exemplo.org/a",
            "options": {"linkPreview": previa}})
        assert status == 200
        _, enviada = ponte.falso.enviadas[0]
        e = enviada.extendedTextMessage
        assert (e.text, e.title, e.description, e.matchedText) == (
            "veja https://exemplo.org/a", "Titulo", "Descricao", "https://exemplo.org/a")
    finally:
        await ponte.http.close()


@pytest.mark.asyncio
async def test_send_message_sem_previa_segue_como_texto_simples(tmp_path, sessoes):
    ponte = await _abrir(tmp_path, sessoes)
    try:
        await ponte.post("send-message", {"phone": [PN], "message": "oi",
                                          "options": {"linkPreview": False}})
        assert ponte.falso.enviadas[0][1] == "oi"
    finally:
        await ponte.http.close()


@pytest.mark.asyncio
async def test_send_reply_leva_citacao_e_cartao_do_link(tmp_path, sessoes):
    ponte = await _abrir(tmp_path, sessoes)
    try:
        ponte.s.bd.mensagem(PN, _msg("ORIG", 100, PN))
        await ponte.post("send-reply", {
            "phone": PN, "message": "https://exemplo.org/b", "messageId": f"false_{PN}_ORIG",
            "options": {"linkPreview": {"title": "T", "canonicalUrl": "https://exemplo.org/b"}}})
        e = ponte.falso.enviadas[0][1].extendedTextMessage
        assert e.contextInfo.stanzaID == "ORIG" and e.title == "T"
        assert e.matchedText == "https://exemplo.org/b"
    finally:
        await ponte.http.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("rota,corpo", [
    ("send-message", {"phone": PN, "message": "oi"}),
    ("send-poll-message", {"phone": PN, "name": "p", "choices": ["a", "b"]}),
])
async def test_erro_do_neonize_vira_json_de_erro_e_nao_texto_simples(
        tmp_path, sessoes, rota, corpo):
    ponte = await _abrir(tmp_path, sessoes)
    try:
        ponte.falso.erro_no_envio = "boom"
        ponte.falso.build_poll_vote_creation = lambda *a: object()
        resposta = await ponte.http.post(ponte.url(rota), json=corpo)
        assert resposta.status == 500
        assert resposta.content_type == "application/json"
        assert (await resposta.json())["status"] == "error"
    finally:
        await ponte.http.close()


# ------------------------------------------------------- apagar para todos
@pytest.mark.asyncio
async def test_admin_revoga_mensagem_de_outro_em_grupo(tmp_path, sessoes):
    ponte = await _abrir(tmp_path, sessoes)
    try:
        grupo = "1203@g.us"
        ponte.s.bd.mensagem(grupo, _msg("ALHEIA", 100, grupo, author="5551@c.us"))
        status, corpo = await ponte.post(
            "delete-message", {"messageId": f"false_{grupo}_ALHEIA_5551@c.us"})
        assert status == 200
        assert ponte.falso.revogadas == [(grupo, "5551@s.whatsapp.net", "ALHEIA")]
    finally:
        await ponte.http.close()


@pytest.mark.asyncio
async def test_revogar_mensagem_de_outro_em_conversa_privada_continua_recusado(tmp_path, sessoes):
    ponte = await _abrir(tmp_path, sessoes)
    try:
        ponte.s.bd.mensagem(PN, _msg("ALHEIA", 100, PN))
        status, _ = await ponte.post("delete-message", {"messageId": f"false_{PN}_ALHEIA"})
        assert status == 500 and ponte.falso.revogadas == []
    finally:
        await ponte.http.close()


@pytest.mark.asyncio
async def test_revogar_mensagem_minha_continua_assinada_por_mim(tmp_path, sessoes):
    ponte = await _abrir(tmp_path, sessoes)
    try:
        ponte.s.bd.mensagem(PN, _msg("MINHA", 100, PN, de_mim=True))
        status, _ = await ponte.post("delete-message", {"messageId": f"true_{PN}_MINHA"})
        assert status == 200
        assert ponte.falso.revogadas == [(PN, EU, "MINHA")]
    finally:
        await ponte.http.close()
