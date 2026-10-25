"""Apagar uma conta da ponte: nunca apaga nada fora da pasta de dados, exige a
credencial da propria sessao e leva junto os bancos e o nome em ativas.json.
Pasta temporaria e sessoes de mentira, sem neonize."""
import json
import os
import sqlite3
from types import SimpleNamespace as N

import pytest
from aiohttp.test_utils import TestClient, TestServer

import autenticacao as aut
import limpeza
import server
from store import Armazem


def _conta(pasta, nome):
    """Uma conta pareada de mentira: os dois bancos e um arquivo qualquer."""
    destino = pasta / nome
    destino.mkdir()
    (destino / "sessao.db").write_bytes(b"chaves")
    (destino / "ponte.db").write_bytes(b"mensagens")
    return destino


# ------------------------------------------------------------ funcoes puras

@pytest.mark.parametrize("nome", ["..", ".", "a/b", "a\\b", "../fora", "", None, "a b",
                                  "/etc", "C:\\x", "a" * 129])
def test_um_nome_que_nao_e_de_sessao_nao_tem_pasta(tmp_path, nome):
    assert limpeza.pasta_da_sessao(str(tmp_path), nome) is None


def test_o_nome_comum_resolve_para_um_filho_direto(tmp_path):
    assert limpeza.pasta_da_sessao(str(tmp_path), "conta-1_a") == os.path.join(
        os.path.realpath(tmp_path), "conta-1_a")


def test_um_atalho_para_fora_nao_e_seguido(tmp_path):
    fora = tmp_path / "fora"
    fora.mkdir()
    (fora / "importante.txt").write_text("nao apague")
    dados = tmp_path / "dados"
    dados.mkdir()
    try:
        os.symlink(fora, dados / "conta1", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("sem link simbolico neste sistema")

    assert limpeza.pasta_da_sessao(str(dados), "conta1") is None
    assert limpeza.apagar_pasta(str(dados), "conta1") is False
    assert (fora / "importante.txt").read_text() == "nao apague"


def test_apagar_leva_a_pasta_da_conta_e_so_ela(tmp_path):
    _conta(tmp_path, "conta1")
    outra = _conta(tmp_path, "conta2")
    (tmp_path / "segredo").write_text("s")

    assert limpeza.apagar_pasta(str(tmp_path), "conta1") is True

    assert not (tmp_path / "conta1").exists()
    assert (outra / "sessao.db").exists() and (tmp_path / "segredo").exists()


def test_apagar_de_novo_uma_pasta_que_ja_nao_existe_termina_o_servico(tmp_path):
    assert limpeza.apagar_pasta(str(tmp_path), "conta1") is True


def test_uma_pasta_que_nao_da_para_apagar_e_dita_como_falha(tmp_path):
    _conta(tmp_path, "conta1")

    def recusa(_destino):
        raise PermissionError("em uso")

    assert limpeza.apagar_pasta(str(tmp_path), "conta1", remover=recusa) is False
    assert (tmp_path / "conta1" / "sessao.db").exists()


def test_o_armazem_fechado_solta_o_arquivo(tmp_path):
    bd = Armazem(str(tmp_path / "ponte.db"))
    bd.fechar()
    with pytest.raises(sqlite3.ProgrammingError):
        bd.conversas()


def test_encerrar_sai_da_conta_para_a_sessao_e_solta_os_bancos():
    chamadas = []
    conta = N(cliente=object(), status="CONNECTED", thread=N(join=lambda t: chamadas.append("join")),
              sair=lambda: chamadas.append("sair"),
              bd=N(fechar=lambda: chamadas.append("fechar")))
    assert limpeza.encerrar(conta) is True
    assert chamadas == ["sair", "join", "fechar"]


def test_encerrar_uma_conta_desconectada_diz_que_nao_desvinculou():
    conta = N(cliente=None, status="CLOSED", thread=None, sair=lambda: None,
              bd=N(fechar=lambda: None))
    assert limpeza.encerrar(conta) is False


# ------------------------------------------------------- o servidor de verdade

async def _servidor(pasta):
    cliente = TestClient(TestServer(server.criar_app(str(pasta))))
    await cliente.start_server()
    segredo = (pasta / aut.ARQUIVO_DO_SEGREDO).read_text(encoding="utf-8")
    return cliente, segredo


def _credencial(segredo, nome):
    return f"{nome}:{aut.token_da_sessao(segredo, nome)}"


@pytest.mark.asyncio
async def test_apagar_a_conta_leva_a_pasta_o_nome_em_ativas_e_a_sessao(tmp_path):
    cliente, segredo = await _servidor(tmp_path)
    try:
        credencial = _credencial(segredo, "conta1")
        # a ponte abre a conta (cria ponte.db) como faria qualquer chamada do app
        assert (await cliente.get(f"/api/{credencial}/status-session")).status == 200
        (tmp_path / "conta1" / "sessao.db").write_bytes(b"chaves")
        outra = _conta(tmp_path, "conta2")
        (tmp_path / "ativas.json").write_text(json.dumps(["conta1", "conta2"]))

        resposta = await cliente.post(f"/api/{credencial}/delete-session")

        assert resposta.status == 200
        assert (await resposta.json())["response"]["removed"] is True
        assert not (tmp_path / "conta1").exists()
        assert json.loads((tmp_path / "ativas.json").read_text()) == ["conta2"]
        assert (outra / "sessao.db").exists()
        listadas = await (await cliente.get(f"/api/{segredo}/show-all-sessions")).json()
        assert listadas["response"] == []
        # pedir de novo (a primeira resposta pode ter se perdido) tambem termina
        assert (await cliente.post(f"/api/{credencial}/delete-session")).status == 200
    finally:
        await cliente.close()


@pytest.mark.asyncio
async def test_sem_credencial_ou_com_a_de_outra_conta_nada_e_apagado(tmp_path):
    cliente, segredo = await _servidor(tmp_path)
    try:
        alvo = _conta(tmp_path, "conta1")

        assert (await cliente.post("/api/conta1/delete-session")).status == 401
        assert (await cliente.post("/api/conta1:errado/delete-session")).status == 401
        de_outra = _credencial(segredo, "conta2")
        assert (await cliente.post(f"/api/conta1:{de_outra.split(':')[1]}/delete-session")
                ).status == 401
        # o segredo da ponte, no lugar do token, tambem nao serve
        assert (await cliente.post(f"/api/{segredo}/delete-session")).status == 401

        assert (alvo / "sessao.db").exists()
    finally:
        await cliente.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("nome", ["..", "a%2Fb", "a%2F..%2F..%2Ffora", "a%5Cb"])
async def test_um_nome_que_foge_da_pasta_e_recusado_mesmo_com_credencial_valida(tmp_path, nome):
    """O hash e conferido sobre o texto do nome; quem tem o segredo (o proprio
    cliente) ainda assim nao pode apontar a rota para fora da pasta de dados."""
    dados = tmp_path / "dados"
    dados.mkdir()
    cliente, segredo = await _servidor(dados)
    try:
        (tmp_path / "importante.txt").write_text("nao apague")
        decodificado = nome.replace("%2F", "/").replace("%5C", "\\")
        credencial = _credencial(segredo, decodificado)

        resposta = await cliente.post(f"/api/{nome}:{credencial.split(':')[1]}/delete-session")

        assert resposta.status in (400, 404)
        assert (tmp_path / "importante.txt").read_text() == "nao apague"
        assert dados.exists()
    finally:
        await cliente.close()
