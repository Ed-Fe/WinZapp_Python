"""send-file e get-media-by-message de verdade (criar_app) contra um cliente
neonize de mentira: os eventos media-upload-progress e media-download-progress
que o WinZapp escuta, e o caminho de antes quando o cliente nao e o do fork.
Sem WhatsApp e sem a DLL do fork."""
import threading
from types import SimpleNamespace as N

import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer
from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import DocumentMessage, ImageMessage, Message

import autenticacao as aut
import server

PN = "5511999990000@s.whatsapp.net"
EU = "5500000000@s.whatsapp.net"
CONTEUDO = b"0123456789"


class ClienteBase:
    def __init__(self):
        self.uploads, self.baixados = [], []

    def upload(self, binario, media_type=None):
        self.uploads.append(("comum", len(binario)))
        return N()

    def send_document(self, to, file, caption=None, filename=None, mimetype=None):
        with open(file, "rb") as f:
            dados = f.read()
        self.upload(dados, "DOC")        # o build_document_message passa o tipo
        return self.send_message(to, Message(documentMessage=DocumentMessage(
            fileName=filename or "", fileLength=len(dados))))

    def send_message(self, destino, mensagem):
        return N(ID="NOVA1", Timestamp=1_700_000_000, Message=mensagem)

    def download_any(self, mensagem):
        self.baixados.append("memoria")
        return CONTEUDO


class ClienteSemFork(ClienteBase):
    """O neonize 0.5.2 de verdade."""


class ClienteFork(ClienteBase):
    def __init__(self, visto):
        super().__init__()
        self.pct, self.visto, self.cancelados = -1, visto, []

    def tem_transferencias(self):
        return True

    def upload_file(self, path, media_type, key):
        self.uploads.append(("arquivo", media_type, key))
        self.pct = 40
        assert self.visto.wait(5)           # so segue depois de o evento sair
        self.pct = 100
        return N()

    def transfer_progress(self, key):
        return self.pct

    def transfer_cancel(self, key):
        self.cancelados.append(key)

    def download_any_to_file(self, message, path, key):
        self.baixados.append(("arquivo", key))
        with open(path, "wb") as f:
            f.write(CONTEUDO[:5])
        assert self.visto.wait(5)
        with open(path, "wb") as f:
            f.write(CONTEUDO)


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
    def __init__(self, http, token, s):
        self.http, self.token, self.s = http, token, s
        self.eventos = []

    def url(self, rota):
        return f"/api/{self.token}/{rota}"


async def _abrir(tmp_path, sessoes, cliente, visto_quando=None):
    http = TestClient(TestServer(server.criar_app(str(tmp_path))))
    await http.start_server()
    segredo = (tmp_path / aut.ARQUIVO_DO_SEGREDO).read_text(encoding="utf-8")
    token = f"conta1:{aut.token_da_sessao(segredo, 'conta1')}"
    await http.get(f"/api/{token}/status-session")
    s = sessoes[0]
    s.cliente, s.status, s.eu = cliente, "CONNECTED", EU
    ponte = Ponte(http, token, s)

    def emitir(evento, dados):
        ponte.eventos.append((evento, dados))
        if visto_quando and visto_quando(evento, dados):
            cliente.visto.set()
    s.emitir = emitir
    return ponte


def _enviar_arquivo(upload_id=None):
    form = FormData()
    form.add_field("phone", PN)
    form.add_field("type", "document")
    if upload_id is not None:
        form.add_field("uploadId", upload_id)
    form.add_field("file", b"x" * 64, filename="a.bin", content_type="application/octet-stream")
    return form


def _guardar_imagem(s):
    s.bd.conversa(PN, name="Ana")
    w = {"id": {"id": "M1", "fromMe": False, "remote": PN, "_serialized": f"false_{PN}_M1"},
         "fromMe": False, "t": 100, "timestamp": 100, "type": "image", "body": "",
         "from": PN, "to": EU}
    s.bd.mensagem(PN, w, Message(imageMessage=ImageMessage(fileLength=len(CONTEUDO))).SerializeToString())
    return f"false_{PN}_M1"


def _de_upload(ponte):
    return [d for e, d in ponte.eventos if e == "media-upload-progress"]


def _de_download(ponte):
    return [d for e, d in ponte.eventos if e == "media-download-progress"]


# -------------------------------------------------------------- send-file
@pytest.mark.asyncio
async def test_send_file_emite_media_upload_progress_com_o_upload_id_do_cliente(tmp_path, sessoes):
    visto = threading.Event()
    cliente = ClienteFork(visto)
    ponte = await _abrir(tmp_path, sessoes, cliente,
                         lambda e, d: e == "media-upload-progress" and d["progress"] == 0.4)
    try:
        resposta = await ponte.http.post(ponte.url("send-file"), data=_enviar_arquivo("LOCAL-7"))
        assert resposta.status == 200
        eventos = _de_upload(ponte)
        assert eventos[0] == {"uploadId": "LOCAL-7", "progress": 0.4, "session": "conta1"}
        assert eventos[-1] == {"uploadId": "LOCAL-7", "progress": 1.0, "session": "conta1"}
        assert cliente.uploads[0][0] == "arquivo"       # subiu pelo disco, nao pelos bytes
        assert not any(u[0] == "comum" for u in cliente.uploads)
    finally:
        await ponte.http.close()


@pytest.mark.asyncio
async def test_send_file_sem_o_fork_envia_como_antes_sem_eventos(tmp_path, sessoes):
    cliente = ClienteSemFork()
    ponte = await _abrir(tmp_path, sessoes, cliente)
    try:
        resposta = await ponte.http.post(ponte.url("send-file"), data=_enviar_arquivo("LOCAL-7"))
        assert resposta.status == 200
        assert cliente.uploads == [("comum", 64)] and ponte.eventos == []
    finally:
        await ponte.http.close()


@pytest.mark.asyncio
async def test_send_file_sem_upload_id_nao_emite_nada(tmp_path, sessoes):
    cliente = ClienteFork(threading.Event())
    ponte = await _abrir(tmp_path, sessoes, cliente)
    try:
        resposta = await ponte.http.post(ponte.url("send-file"), data=_enviar_arquivo())
        assert resposta.status == 200
        assert cliente.uploads == [("comum", 64)] and ponte.eventos == []
    finally:
        await ponte.http.close()


# -------------------------------------------------------------- get-media
@pytest.mark.asyncio
async def test_get_media_emite_media_download_progress_com_o_progress_id(tmp_path, sessoes):
    visto = threading.Event()
    cliente = ClienteFork(visto)
    ponte = await _abrir(tmp_path, sessoes, cliente,
                         lambda e, d: e == "media-download-progress" and d["progress"] == 0.5)
    try:
        serial = _guardar_imagem(ponte.s)
        resposta = await ponte.http.post(
            ponte.url(f"get-media-by-message/{serial}"), json={"progressId": "M1"},
            headers={"Accept": "application/octet-stream"})
        assert resposta.status == 200 and await resposta.read() == CONTEUDO
        eventos = _de_download(ponte)
        assert eventos[0] == {"progressId": "M1", "progress": 0.5, "session": "conta1"}
        assert eventos[-1] == {"progressId": "M1", "progress": 1.0, "session": "conta1"}
        assert cliente.baixados[0][0] == "arquivo"
    finally:
        await ponte.http.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cliente_de,corpo", [
    (lambda: ClienteSemFork(), {"progressId": "M1"}),          # sem o fork
    (lambda: ClienteFork(threading.Event()), None),            # sem corpo
    (lambda: ClienteFork(threading.Event()), {"x": 1}),        # sem progressId
])
async def test_get_media_cai_no_download_de_antes_sem_eventos(tmp_path, sessoes, cliente_de, corpo):
    cliente = cliente_de()
    ponte = await _abrir(tmp_path, sessoes, cliente)
    try:
        serial = _guardar_imagem(ponte.s)
        resposta = await ponte.http.post(
            ponte.url(f"get-media-by-message/{serial}"), json=corpo,
            headers={"Accept": "application/octet-stream"})
        assert resposta.status == 200 and await resposta.read() == CONTEUDO
        assert cliente.baixados == ["memoria"] and ponte.eventos == []
    finally:
        await ponte.http.close()


@pytest.mark.asyncio
async def test_get_media_com_erro_do_download_continua_respondendo_410(tmp_path, sessoes):
    visto = threading.Event()
    cliente = ClienteFork(visto)

    def falha(*a):
        raise RuntimeError("404 not found")
    cliente.download_any_to_file = falha
    ponte = await _abrir(tmp_path, sessoes, cliente)
    try:
        serial = _guardar_imagem(ponte.s)
        resposta = await ponte.http.post(
            ponte.url(f"get-media-by-message/{serial}"), json={"progressId": "M1"})
        assert resposta.status == 410
    finally:
        await ponte.http.close()
