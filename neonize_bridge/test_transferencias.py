"""Envio e download com progresso contra clientes falsos: um que expoe as
chamadas do fork da DLL e um que nao (o neonize 0.5.2 de verdade, o que o CI
instala). Nada aqui carrega a DLL do fork."""
import asyncio
import os
import threading
from types import SimpleNamespace as N

import pytest

import transferencias as t


class ClienteBase:
    """O formato do NewClient: send_image -> build_image_message -> upload."""

    def __init__(self):
        self.uploads, self.enviadas, self.baixados = [], [], []

    def upload(self, binario, media_type=None):
        self.uploads.append(("comum", len(binario), media_type))
        return N(origem="upload")

    def build_image_message(self, file, caption=None):
        with open(file, "rb") as f:
            dados = f.read()
        return N(upload=self.upload(dados), caption=caption)

    def send_image(self, to, file, caption=None):
        return self.send_message(to, self.build_image_message(file, caption))

    def send_message(self, to, mensagem):
        self.enviadas.append((to, mensagem))
        return N(ID="X1", mensagem=mensagem)

    def download_any(self, mensagem):
        self.baixados.append(("memoria", mensagem))
        return b"dados"


class ClienteSemFork(ClienteBase):
    """O neonize 0.5.2 de verdade: nada de upload_file nem transfer_*."""


class ClienteFork(ClienteBase):
    def __init__(self, conteudo_baixado=b"abcdefghij", tem=True):
        super().__init__()
        self.tem, self.pct = tem, -1
        self.liberar = threading.Event()
        self.chegou = threading.Event()
        self.cancelados, self.conteudo = [], conteudo_baixado
        self.escrever = None          # callback(path) no meio do download

    def tem_transferencias(self):
        return self.tem

    def upload_file(self, path, media_type, key):
        self.uploads.append(("arquivo", path, media_type, key))
        self.chegou.set()
        assert self.liberar.wait(5)
        return N(origem="upload_file")

    def transfer_progress(self, key):
        return self.pct

    def transfer_cancel(self, key):
        self.cancelados.append(key)

    def download_any_to_file(self, message, path, key):
        self.baixados.append(("arquivo", key))
        if self.escrever:
            self.escrever(path)
        self.chegou.set()
        assert self.liberar.wait(5)
        with open(path, "wb") as f:
            f.write(self.conteudo)


def _espera(condicao, tempo=5):
    fim = threading.Event()
    for _ in range(int(tempo / 0.01)):
        if condicao():
            return
        fim.wait(0.01)
    raise AssertionError("nao aconteceu")


def _png(tmp_path, tamanho=64):
    caminho = tmp_path / "a.png"
    caminho.write_bytes(b"x" * tamanho)
    return str(caminho)


def _tipo(_binario):
    return "TIPO_PELO_CONTEUDO"


# ------------------------------------------------------------ deteccao
def test_tem_transferencias_exige_as_quatro_chamadas_e_a_dll():
    assert t.tem_transferencias(ClienteFork()) is True
    assert t.tem_transferencias(ClienteFork(tem=False)) is False      # DLL sem os exports
    assert t.tem_transferencias(ClienteSemFork()) is False            # neonize de verdade
    parcial = ClienteFork()
    parcial.transfer_cancel = None
    assert t.tem_transferencias(parcial) is False


def test_tem_transferencias_com_dll_que_levanta_e_falso():
    c = ClienteFork()
    c.tem_transferencias = lambda: (_ for _ in ()).throw(OSError("dll"))
    assert t.tem_transferencias(c) is False


def test_tamanho_esperado_pega_o_filelength_da_midia():
    m = N(imageMessage=N(fileLength=0), videoMessage=N(fileLength=2048))
    assert t.tamanho_esperado(m) == 2048
    assert t.tamanho_esperado(N(documentMessage=N(fileLength="x"))) == 0
    assert t.tamanho_esperado(N()) == 0


def test_tamanho_esperado_com_protobuf_de_verdade():
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import DocumentMessage, Message
    m = Message(documentMessage=DocumentMessage(fileLength=777))
    assert t.tamanho_esperado(m) == 777
    assert t.tamanho_esperado(Message(conversation="oi")) == 0


# ------------------------------------------------------------ cliente com progresso
def test_o_proxy_manda_o_upload_do_arquivo_pelo_disco(tmp_path):
    c = ClienteFork()
    c.liberar.set()
    caminho = _png(tmp_path)
    proxy = t.ClienteComProgresso(c, caminho, "up-k", tipo_pelo_conteudo=_tipo)
    resp = proxy.send_image("dest", caminho, caption="oi")
    # build_image_message e send_message rodaram, mas o upload foi o do fork
    assert c.uploads == [("arquivo", caminho, "TIPO_PELO_CONTEUDO", "up-k")]
    assert resp.mensagem.upload.origem == "upload_file"
    assert c.enviadas[0][0] == "dest"


def test_o_proxy_deixa_passar_upload_de_outro_tamanho_e_o_tipo_dado(tmp_path):
    c = ClienteFork()
    caminho = _png(tmp_path, 64)
    proxy = t.ClienteComProgresso(c, caminho, "k", tipo_pelo_conteudo=_tipo)
    proxy.upload(b"miniatura", "TIPO")          # nao e o arquivo: caminho comum
    assert c.uploads == [("comum", 9, "TIPO")]
    c.liberar.set()
    proxy.upload(b"x" * 64, "DOC")               # e o arquivo, com o tipo dado
    assert c.uploads[1] == ("arquivo", caminho, "DOC", "k")


def test_o_proxy_com_arquivo_que_sumiu_usa_o_upload_comum(tmp_path):
    c = ClienteFork()
    proxy = t.ClienteComProgresso(c, str(tmp_path / "nao-existe"), "k", tipo_pelo_conteudo=_tipo)
    proxy.upload(b"abc")
    assert c.uploads == [("comum", 3, None)]


def test_o_proxy_repassa_atributos_ao_cliente_de_verdade(tmp_path):
    c = ClienteFork()
    c.uuid = "u-1"
    proxy = t.ClienteComProgresso(c, _png(tmp_path), "k")
    assert proxy.uuid == "u-1"


# ------------------------------------------------------------ upload
def test_upload_emite_o_progresso_real_e_termina_em_100(tmp_path):
    c = ClienteFork()
    eventos = []
    caminho = _png(tmp_path)
    resultado = []

    def enviar(cliente):
        return cliente.send_image("dest", caminho)

    def rodar():
        resultado.append(t.enviar_com_progresso(
            c, caminho, "UP1", lambda e, d: eventos.append((e, d)), "conta1", enviar,
            intervalo=0.01, minimo=0.0))
    fio = threading.Thread(target=rodar)
    fio.start()
    assert c.chegou.wait(5)
    c.pct = 10
    _espera(lambda: any(d["progress"] == 0.1 for _, d in eventos))
    c.pct = 55
    _espera(lambda: any(d["progress"] == 0.55 for _, d in eventos))
    c.liberar.set()
    fio.join(5)
    assert resultado and resultado[0].ID == "X1"
    assert all(e == "media-upload-progress" for e, _ in eventos)
    assert all(d["uploadId"] == "UP1" and d["session"] == "conta1" for _, d in eventos)
    progressos = [d["progress"] for _, d in eventos]
    assert progressos == sorted(progressos) and progressos[-1] == 1.0


def test_upload_sem_as_chamadas_do_fork_envia_como_antes_sem_eventos(tmp_path):
    c = ClienteSemFork()
    eventos = []
    caminho = _png(tmp_path)
    resp = t.enviar_com_progresso(
        c, caminho, "UP1", lambda e, d: eventos.append(e), "conta1",
        lambda cliente: cliente.send_image("dest", caminho))
    assert resp.ID == "X1"
    assert c.uploads == [("comum", 64, None)] and eventos == []


def test_upload_sem_id_ou_com_bytes_nao_usa_o_fork(tmp_path):
    c = ClienteFork()
    usados = []
    for upload_id, fonte in (("", _png(tmp_path)), ("UP1", b"bytes")):
        t.enviar_com_progresso(c, fonte, upload_id, lambda *a: None, "s",
                               lambda cliente: usados.append(cliente))
    assert usados == [c, c]          # o cliente de verdade, sem proxy
    assert c.uploads == []


def test_upload_que_falha_nao_emite_100_e_propaga(tmp_path):
    c = ClienteFork()
    eventos = []
    caminho = _png(tmp_path)

    def enviar(cliente):
        raise RuntimeError("rede caiu")
    with pytest.raises(RuntimeError):
        t.enviar_com_progresso(c, caminho, "UP1", lambda e, d: eventos.append(d), "s",
                               enviar, intervalo=0.01, minimo=0.0)
    assert eventos == []


@pytest.mark.asyncio
async def test_requisicao_cancelada_cancela_o_upload_na_dll(tmp_path):
    c = ClienteFork()
    caminho = _png(tmp_path)
    tarefa = asyncio.ensure_future(t.enviar_com_progresso_async(
        c, caminho, "UP1", lambda *a: None, "s",
        lambda cliente: cliente.send_image("dest", caminho)))
    await asyncio.get_running_loop().run_in_executor(None, c.chegou.wait, 5)
    tarefa.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tarefa
    assert len(c.cancelados) == 1 and c.cancelados[0].startswith("up-")
    c.liberar.set()         # solta a thread do upload falso


# ------------------------------------------------------------ download
def test_download_mede_o_arquivo_contra_o_filelength(tmp_path):
    c = ClienteFork(conteudo_baixado=b"0123456789")
    eventos = []
    mensagem = N(imageMessage=N(fileLength=10))
    gravado = threading.Event()

    def meio(path):
        with open(path, "wb") as f:
            f.write(b"01234")             # metade
        gravado.set()
    c.escrever = meio
    resultado = []
    fio = threading.Thread(target=lambda: resultado.append(t.baixar_com_progresso(
        c, mensagem, "DL1", lambda e, d: eventos.append((e, d)), "conta1",
        intervalo=0.01, minimo=0.0)))
    fio.start()
    assert gravado.wait(5)
    _espera(lambda: any(d["progress"] == 0.5 for _, d in eventos))
    c.liberar.set()
    fio.join(5)
    assert resultado == [b"0123456789"]
    assert all(e == "media-download-progress" for e, _ in eventos)
    assert all(d["progressId"] == "DL1" and d["session"] == "conta1" for _, d in eventos)
    assert eventos[-1][1]["progress"] == 1.0
    assert c.baixados[0][0] == "arquivo"


def test_download_apaga_o_arquivo_temporario(tmp_path, monkeypatch):
    c = ClienteFork(conteudo_baixado=b"a" * 10)
    c.liberar.set()
    criados = []
    original = t.tempfile.mkstemp

    def mkstemp(*a, **kw):
        d, caminho = original(*a, dir=str(tmp_path), **kw)
        criados.append(caminho)
        return d, caminho
    monkeypatch.setattr(t.tempfile, "mkstemp", mkstemp)
    t.baixar_com_progresso(c, N(imageMessage=N(fileLength=10)), "DL1", lambda *a: None, "s")
    assert len(criados) == 1 and not os.path.exists(criados[0])


def test_download_que_falha_apaga_o_temporario_e_propaga(tmp_path, monkeypatch):
    c = ClienteFork()
    criados = []
    original = t.tempfile.mkstemp

    def mkstemp(*a, **kw):
        d, caminho = original(*a, dir=str(tmp_path), **kw)
        criados.append(caminho)
        return d, caminho
    monkeypatch.setattr(t.tempfile, "mkstemp", mkstemp)

    def falha(*a):
        raise RuntimeError("404 not found")
    c.download_any_to_file = falha
    with pytest.raises(RuntimeError, match="404"):
        t.baixar_com_progresso(c, N(imageMessage=N(fileLength=10)), "DL1", lambda *a: None, "s")
    assert criados and not os.path.exists(criados[0])


@pytest.mark.parametrize("cliente,mensagem,progress_id", [
    (ClienteSemFork(), N(imageMessage=N(fileLength=10)), "DL1"),       # sem o fork
    (ClienteFork(tem=False), N(imageMessage=N(fileLength=10)), "DL1"),  # DLL sem exports
    (ClienteFork(), N(imageMessage=N(fileLength=10)), ""),             # sem progressId
    (ClienteFork(), N(imageMessage=N(fileLength=0)), "DL1"),           # tamanho desconhecido
])
def test_download_cai_no_download_any_de_antes_sem_eventos(cliente, mensagem, progress_id):
    eventos = []
    dados = t.baixar_com_progresso(cliente, mensagem, progress_id,
                                   lambda e, d: eventos.append(e), "s")
    assert dados == b"dados" and eventos == []
    assert cliente.baixados == [("memoria", mensagem)]


@pytest.mark.asyncio
async def test_download_async_le_o_progress_id_do_corpo_e_cancela_na_dll():
    c = ClienteFork()
    eventos = []
    mensagem = N(imageMessage=N(fileLength=10))
    tarefa = asyncio.ensure_future(t.baixar_com_progresso_async(
        c, mensagem, {"progressId": "DL9"}, lambda e, d: eventos.append(d), "s"))
    await asyncio.get_running_loop().run_in_executor(None, c.chegou.wait, 5)
    tarefa.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tarefa
    assert len(c.cancelados) == 1 and c.cancelados[0].startswith("down-")
    c.liberar.set()


@pytest.mark.asyncio
async def test_download_async_sem_corpo_usa_o_caminho_antigo():
    c = ClienteFork()
    dados = await t.baixar_com_progresso_async(
        c, N(imageMessage=N(fileLength=10)), None, lambda *a: None, "s")
    assert dados == b"dados"
