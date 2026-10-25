"""Rotas de contato, perfil e presenca do servidor de verdade (criar_app), com um
cliente neonize de mentira: o que o cliente do WinZapp le (contact/pn-lid,
blocklist, all-contacts, contact/:phone, subscribe-presence, set-online-presence,
set-profile-pic, join-code). Sem WhatsApp; nada aqui foi provado contra uma conta real."""
import io
from types import SimpleNamespace as N

import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

import autenticacao as aut
import server

FONE = "5511999990000"
LID = "123456789012345"


def jid(usuario, servidor="s.whatsapp.net"):
    return N(User=usuario, Server=servidor)


class ClienteFalso:
    """So o que as rotas tocam; cada chamada fica em `chamadas`."""

    def __init__(self):
        self.chamadas = []
        self.bloqueados = []
        self.pn_do_lid = {}         # usuario do LID -> usuario do numero
        self.lid_do_pn = {}
        self.negocios = {}          # usuario do numero -> BusinessName
        self.falha = None           # excecao que a proxima chamada levanta
        self.contact = N(get_contact=self._get_contact)

    def _talvez_falhar(self):
        if self.falha is not None:
            raise self.falha

    def get_blocklist(self):
        self._talvez_falhar()
        return N(JIDs=self.bloqueados)

    def get_pn_from_lid(self, j):
        if j.User not in self.pn_do_lid:
            raise RuntimeError("sem mapa")
        return jid(self.pn_do_lid[j.User])

    def get_lid_from_pn(self, j):
        if j.User not in self.lid_do_pn:
            raise RuntimeError("sem mapa")
        return jid(self.lid_do_pn[j.User], "lid")

    def _get_contact(self, j):
        return N(BusinessName=self.negocios.get(j.User, ""))

    def subscribe_presence(self, j):
        self.chamadas.append(("subscribe", j.User, j.Server))
        self._talvez_falhar()

    def send_presence(self, presenca):
        self.chamadas.append(("presenca", presenca.value))
        self._talvez_falhar()

    def set_profile_photo(self, dados):
        self.chamadas.append(("foto", bytes(dados[:3])))
        self._talvez_falhar()

    def get_group_info_from_link(self, codigo):
        return N(JID=jid("1203", "g.us"))

    def join_group_with_link(self, codigo):
        self._talvez_falhar()


@pytest.fixture
def sessoes(monkeypatch):
    criadas = []

    class Registrada(server.Sessao):
        def __init__(self, *args, **kw):
            super().__init__(*args, **kw)
            criadas.append(self)
    monkeypatch.setattr(server, "Sessao", Registrada)
    return criadas


async def _ponte(pasta, sessoes, conectada=True):
    """(cliente http, prefixo da API, sessao, cliente neonize de mentira)."""
    http = TestClient(TestServer(server.criar_app(str(pasta))))
    await http.start_server()
    segredo = (pasta / aut.ARQUIVO_DO_SEGREDO).read_text(encoding="utf-8")
    token = f"conta:{aut.token_da_sessao(segredo, 'conta')}"
    assert (await http.get(f"/api/{token}/status-session")).status == 200
    s = sessoes[0]
    falso = ClienteFalso()
    s.cliente = falso
    s.status = "CONNECTED" if conectada else "CLOSED"
    s.eu, s.lid = "5511888880000@s.whatsapp.net", "999888777666555@lid"
    return http, f"/api/{token}", s, falso


@pytest.mark.asyncio
async def test_pn_lid_devolve_o_que_o_cliente_le(tmp_path, sessoes):
    http, api, s, falso = await _ponte(tmp_path, sessoes)
    try:
        falso.pn_do_lid[LID] = FONE
        s.bd.contato(f"{FONE}@s.whatsapp.net", name="Ana Silva", pushname="aninha")
        resposta = await http.get(f"{api}/contact/pn-lid/{LID}@lid")
        corpo = await resposta.json()
        assert resposta.status == 200
        # identity.py::resolve_lid_jids_via_api: phoneNumber/contact; resolve_self_lid: lid/phone
        assert corpo["phoneNumber"] == {"_serialized": f"{FONE}@c.us"}
        assert corpo["contact"] == {"name": "Ana Silva", "pushname": "aninha"}
        assert corpo["lid"] == {"_serialized": f"{LID}@lid"}
        assert corpo["phone"] == corpo["id"] == {"_serialized": f"{FONE}@c.us"}
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_blocklist_com_falha_nao_vira_lista_vazia(tmp_path, sessoes):
    http, api, s, falso = await _ponte(tmp_path, sessoes)
    try:
        falso.falha = RuntimeError("timeout")
        resposta = await http.get(f"{api}/blocklist")
        assert resposta.status == 502
        assert (await resposta.json())["status"] == "error"
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_blocklist_sem_conexao_nao_vira_lista_vazia(tmp_path, sessoes):
    http, api, s, falso = await _ponte(tmp_path, sessoes, conectada=False)
    try:
        resposta = await http.get(f"{api}/blocklist")
        assert resposta.status == 503
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_blocklist_traz_o_numero_de_quem_foi_bloqueado_pelo_lid(tmp_path, sessoes):
    http, api, s, falso = await _ponte(tmp_path, sessoes)
    try:
        falso.pn_do_lid[LID] = FONE
        falso.bloqueados = [jid(LID, "lid"), jid("5511777770000"), jid("555000000000000", "lid")]
        resposta = await http.get(f"{api}/blocklist")
        itens = (await resposta.json())["response"]
        assert resposta.status == 200
        # o cliente le `phone` como telefone: o LID vira o numero; o LID fica em `id`
        assert itens[0] == {"phone": FONE, "id": {"_serialized": f"{LID}@lid"}}
        assert itens[1]["phone"] == "5511777770000"
        # LID sem mapa: ao menos os digitos do proprio LID (casam com a conversa do LID)
        assert itens[2]["phone"] == "555000000000000"
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_all_contacts_diz_se_esta_na_agenda_e_quem_sou_eu(tmp_path, sessoes):
    http, api, s, falso = await _ponte(tmp_path, sessoes)
    try:
        s.bd.contato("5511777770000@s.whatsapp.net", name="Bia")
        s.bd.contato("5511666660000@s.whatsapp.net", pushname="so apelido")
        s.bd.contato(s.eu, pushname="eu")
        linhas = {c["id"]["_serialized"]: c for c in
                  (await (await http.get(f"{api}/all-contacts")).json())["response"]}
        assert linhas["5511777770000@c.us"]["syncToAddressbook"] is True
        assert linhas["5511777770000@c.us"]["isMyContact"] is True
        assert linhas["5511666660000@c.us"]["syncToAddressbook"] is False
        assert linhas["5511666660000@c.us"]["isMe"] is False
        assert linhas["5511888880000@c.us"]["isMe"] is True
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_contact_traz_conta_comercial_e_o_apelido_guardado_so_sob_o_lid(tmp_path, sessoes):
    http, api, s, falso = await _ponte(tmp_path, sessoes)
    try:
        falso.negocios[FONE] = "Loja do Ze"
        falso.lid_do_pn[FONE] = LID
        s.bd.contato(f"{LID}@lid", pushname="ze")
        corpo = (await (await http.get(f"{api}/contact/{FONE}@c.us")).json())["response"]
        assert corpo["isBusiness"] is True and corpo["verifiedName"] == "Loja do Ze"
        assert corpo["pushname"] == "ze"        # estava guardado so sob o LID

        pessoa = (await (await http.get(f"{api}/contact/5511555550000@c.us")).json())["response"]
        assert pessoa["isBusiness"] is False and "verifiedName" not in pessoa
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_contact_acha_o_nome_guardado_na_outra_forma_do_numero_brasileiro(tmp_path, sessoes):
    http, api, s, falso = await _ponte(tmp_path, sessoes)
    try:
        s.bd.contato("551199990000@s.whatsapp.net", name="Cris")     # sem o 9o digito
        corpo = (await (await http.get(f"{api}/contact/{FONE}@c.us")).json())["response"]
        assert corpo["name"] == "Cris" and corpo["isMyContact"] is True
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_subscribe_presence_so_confirma_o_que_o_whatsapp_aceitou(tmp_path, sessoes):
    http, api, s, falso = await _ponte(tmp_path, sessoes)
    try:
        boa = await http.post(f"{api}/subscribe-presence", json={"phone": f"{FONE}@c.us"})
        assert boa.status == 200 and (await boa.json())["status"] == "success"
        assert falso.chamadas == [("subscribe", FONE, "s.whatsapp.net")]
        assert s.aberta == ""       # o cliente assina muitos contatos: nao e a conversa aberta

        falso.falha = RuntimeError("recusado")
        recusada = await http.post(f"{api}/subscribe-presence", json={"phone": f"{LID}@lid"})
        assert recusada.status == 502 and (await recusada.json())["status"] == "error"
        invalido = await http.post(f"{api}/subscribe-presence", json={"phone": "status@broadcast"})
        assert invalido.status == 400
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_subscribe_presence_sem_conexao_nao_confirma(tmp_path, sessoes):
    http, api, s, falso = await _ponte(tmp_path, sessoes, conectada=False)
    try:
        resposta = await http.post(f"{api}/subscribe-presence", json={"phone": f"{FONE}@c.us"})
        assert resposta.status == 503
        assert falso.chamadas == []
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_set_online_presence_envia_a_presenca_ao_whatsapp(tmp_path, sessoes):
    http, api, s, falso = await _ponte(tmp_path, sessoes)
    try:
        assert (await http.post(f"{api}/set-online-presence", json={"isOnline": True})).status == 200
        assert (await http.post(f"{api}/set-online-presence", json={"isOnline": False})).status == 200
        assert falso.chamadas == [("presenca", b"available"), ("presenca", b"unavailable")]
        falso.falha = RuntimeError("sem rede")
        assert (await http.post(f"{api}/set-online-presence", json={"isOnline": True})).status == 502
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_set_online_presence_sem_conexao_so_guarda_o_estado(tmp_path, sessoes):
    http, api, s, falso = await _ponte(tmp_path, sessoes, conectada=False)
    try:
        assert (await http.post(f"{api}/set-online-presence", json={"isOnline": True})).status == 200
        assert s.online is True and falso.chamadas == []
    finally:
        await http.close()


def _png():
    from PIL import Image
    saida = io.BytesIO()
    Image.new("RGB", (32, 32), "red").save(saida, format="PNG")
    return saida.getvalue()


async def _enviar_foto(http, api, dados, tipo):
    form = FormData()
    form.add_field("file", dados, filename="foto", content_type=tipo)
    return await http.post(f"{api}/set-profile-pic", data=form)


@pytest.mark.asyncio
async def test_set_profile_pic_converte_png_para_jpeg(tmp_path, sessoes):
    http, api, s, falso = await _ponte(tmp_path, sessoes)
    try:
        resposta = await _enviar_foto(http, api, _png(), "image/png")
        assert resposta.status == 200
        assert falso.chamadas == [("foto", b"\xff\xd8\xff")]
        assert (await _enviar_foto(http, api, b"nao e imagem", "image/png")).status == 415
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_join_code_so_diz_pedido_pendente_quando_o_erro_diz(tmp_path, sessoes):
    http, api, s, falso = await _ponte(tmp_path, sessoes)
    try:
        corpo = {"inviteCode": "ABCDEFGHIJ1234"}
        assert (await (await http.post(f"{api}/join-code", json=corpo)).json())[
            "response"]["pendingApproval"] is False

        falso.falha = RuntimeError("info query returned status 400: bad-request")
        falhou = await http.post(f"{api}/join-code", json=corpo)
        assert falhou.status == 500 and (await falhou.json())["status"] == "error"

        falso.falha = RuntimeError("membership_approval_request")
        pendente = await http.post(f"{api}/join-code", json=corpo)
        assert pendente.status == 200
        assert (await pendente.json())["response"]["pendingApproval"] is True

        falso.falha = RuntimeError("info query returned status 409: conflict")
        assert (await http.post(f"{api}/join-code", json=corpo)).status == 409
    finally:
        await http.close()
