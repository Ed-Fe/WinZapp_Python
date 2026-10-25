"""Envio e download de midia com progresso real, usando as chamadas do fork da
DLL (`0.5.2-winzapp-teste.2`, ver preparar.py): `upload_file`,
`transfer_progress`, `transfer_cancel` e `download_any_to_file`.

O neonize 0.5.2 de verdade (o que o CI instala) nao as tem. Tudo aqui checa
`tem_transferencias` e, sem elas, cai no caminho de antes (`download_any`, o
envio comum) sem emitir evento nenhum.

Upload: o `build_*_message` do neonize chama `self.upload(bytes)`. Em vez de
reescrever as quatro montagens de mensagem (miniatura, duracao, forma de onda),
`ClienteComProgresso` roda os mesmos metodos do cliente com `upload` trocado por
`upload_file` (que le do disco e informa o progresso: 0-10% cifrando, 10-99% na
rede). Download: o progresso da DLL fica em 0, entao a ponte mede o tamanho do
arquivo de saida contra o `fileLength` da mensagem.

Nada aqui foi testado contra a DLL de verdade (so contra fakes); a logica pura
fica em progresso.py. Testado em test_transferencias.py.
"""
import asyncio
import inspect
import logging
import os
import tempfile
import types
import uuid

import progresso

log = logging.getLogger("ponte")

#: os campos de Message que carregam `fileLength` de uma midia
CAMPOS_DE_MIDIA = ("imageMessage", "videoMessage", "audioMessage", "documentMessage",
                   "stickerMessage", "ptvMessage")


def tem_transferencias(cliente):
    """True quando o cliente e o do fork E a DLL carregada exporta as chamadas
    de transferencia (`tem_transferencias()` do fork olha a DLL)."""
    detectar = getattr(cliente, "tem_transferencias", None)
    if not callable(detectar):
        return False
    for nome in ("upload_file", "transfer_progress", "transfer_cancel",
                 "download_any_to_file"):
        if not callable(getattr(cliente, nome, None)):
            return False
    try:
        return bool(detectar())
    except Exception:
        return False


def chave_nova(prefixo):
    """Chave de uma transferencia na DLL (unica, nunca o id que o cliente deu)."""
    return "%s-%s" % (prefixo, uuid.uuid4().hex)


def tamanho_esperado(mensagem):
    """`fileLength` da midia da mensagem, 0 quando nao ha."""
    for campo in CAMPOS_DE_MIDIA:
        midia = getattr(mensagem, campo, None)
        try:
            tamanho = int(getattr(midia, "fileLength", 0) or 0)
        except (TypeError, ValueError):
            continue
        if tamanho > 0:
            return tamanho
    return 0


def _tipo_pelo_conteudo(binario):
    from neonize.utils.enum import MediaType
    return MediaType.from_magic(binario)


class ClienteComProgresso:
    """O cliente do neonize com `upload` do arquivo `caminho` trocado por
    `upload_file`. Os metodos do cliente sao religados a este objeto, entao
    `build_*_message` e `send_*` chamam o `upload` daqui; o resto (atributos,
    o `__client` interno) vai para o cliente de verdade."""

    def __init__(self, cliente, caminho, chave, tipo_pelo_conteudo=_tipo_pelo_conteudo):
        self._cliente, self._caminho, self._chave = cliente, caminho, chave
        self._tipo = tipo_pelo_conteudo
        try:
            self._tamanho = os.path.getsize(caminho)
        except OSError:
            self._tamanho = -1

    def upload(self, binario, media_type=None):
        # so o upload do proprio arquivo vai pelo disco; miniaturas e afins
        # (outro tamanho) seguem o caminho comum
        if self._tamanho < 0 or len(binario) != self._tamanho:
            return self._cliente.upload(binario, media_type)
        return self._cliente.upload_file(
            self._caminho, media_type or self._tipo(binario), self._chave)

    def __getattr__(self, nome):
        # so chega aqui o que a classe nao define
        funcao = getattr(type(self._cliente), nome, None)
        if inspect.isfunction(funcao):
            return types.MethodType(funcao, self)
        return getattr(self._cliente, nome)


def enviar_com_progresso(cliente, caminho, upload_id, emitir_evento, sessao, enviar,
                         chave=None, intervalo=progresso.INTERVALO_LEITURA,
                         minimo=progresso.INTERVALO_MINIMO):
    """Roda `enviar(c)` (bloqueante; `c` e o cliente a usar) e, quando da,
    emite `media-upload-progress` enquanto o arquivo sobe. Sem `upload_id`, sem
    caminho de disco ou sem as chamadas do fork, e so `enviar(cliente)`."""
    if not (upload_id and isinstance(caminho, str) and tem_transferencias(cliente)):
        return enviar(cliente)
    chave = chave or chave_nova("up")
    proxy = ClienteComProgresso(cliente, caminho, chave)

    def ler():
        return progresso.fracao_de_percentual(cliente.transfer_progress(chave))

    def emitir(fracao):
        emitir_evento("media-upload-progress",
                      progresso.evento_upload(sessao, upload_id, fracao))
    with progresso.Acompanhante(ler, emitir, intervalo=intervalo, minimo=minimo) as acompanhante:
        resposta = enviar(proxy)
        acompanhante.concluir()
    return resposta


async def enviar_com_progresso_async(cliente, caminho, upload_id, emitir_evento, sessao,
                                     enviar):
    """`enviar_com_progresso` numa thread; se a requisicao for cancelada (o
    cliente desistiu), cancela o upload na DLL."""
    chave = chave_nova("up")
    laco = asyncio.get_running_loop()
    try:
        return await laco.run_in_executor(
            None, lambda: enviar_com_progresso(
                cliente, caminho, upload_id, emitir_evento, sessao, enviar, chave))
    except asyncio.CancelledError:
        _cancelar(cliente, chave)
        raise


def _cancelar(cliente, chave):
    if not tem_transferencias(cliente):
        return
    try:
        cliente.transfer_cancel(chave)
    except Exception:
        log.exception("transfer_cancel")


def baixar_com_progresso(cliente, mensagem, progress_id, emitir_evento, sessao,
                         chave=None, intervalo=progresso.INTERVALO_LEITURA,
                         minimo=progresso.INTERVALO_MINIMO):
    """Bytes da midia de `mensagem`. Com as chamadas do fork, um `progress_id` e
    o tamanho conhecido, baixa para um arquivo temporario medindo o tamanho
    dele e emite `media-download-progress`; senao, `download_any` como antes."""
    esperado = tamanho_esperado(mensagem)
    if not (progress_id and esperado > 0 and tem_transferencias(cliente)):
        return cliente.download_any(mensagem)
    chave = chave or chave_nova("down")
    descritor, caminho = tempfile.mkstemp(prefix="winzapp-dl-")
    os.close(descritor)

    def ler():
        try:
            return progresso.fracao_de_arquivo(os.path.getsize(caminho), esperado)
        except OSError:
            return None

    def emitir(fracao):
        emitir_evento("media-download-progress",
                      progresso.evento_download(sessao, progress_id, fracao))
    try:
        with progresso.Acompanhante(ler, emitir, intervalo=intervalo, minimo=minimo) as acompanhante:
            cliente.download_any_to_file(mensagem, caminho, chave)
            acompanhante.concluir()
        with open(caminho, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(caminho)
        except OSError:
            pass


async def baixar_com_progresso_async(cliente, mensagem, corpo, emitir_evento, sessao):
    """`baixar_com_progresso` numa thread (o `progressId` vem do `corpo` JSON da
    requisicao, se houver); requisicao cancelada cancela o download."""
    progress_id = progresso.id_de_progresso(corpo)
    chave = chave_nova("down")
    laco = asyncio.get_running_loop()
    try:
        return await laco.run_in_executor(
            None, lambda: baixar_com_progresso(
                cliente, mensagem, progress_id, emitir_evento, sessao, chave))
    except asyncio.CancelledError:
        _cancelar(cliente, chave)
        raise
