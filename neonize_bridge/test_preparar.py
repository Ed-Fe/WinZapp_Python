"""preparar.py: o zip da build so pode trocar arquivos do pacote neonize."""
import io
import zipfile

import pytest

import preparar


def zip_com(*membros, links=()):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for nome in membros:
            z.writestr(nome, b"x")
        for nome in links:
            info = zipfile.ZipInfo(nome)
            info.external_attr = 0o120777 << 16
            z.writestr(info, b"alvo")
    buf.seek(0)
    return zipfile.ZipFile(buf)


def problemas(*membros, **kw):
    return preparar.check_members(zip_com(*membros, **kw).infolist())


def test_um_pacote_neonize_comum_passa():
    assert problemas("neonize/__init__.py", "neonize/client.py", "neonize/client.pyi",
                     "neonize/proto/Neonize_pb2.py", "neonize/neonize-windows-amd64.dll",
                     "neonize/__pycache__/client.cpython-313.pyc", "neonize/py.typed") == []


@pytest.mark.parametrize("nome", [
    "evil.pth", "sitecustomize.py", "usercustomize.py", "outro_pacote/__init__.py",
    "neonize/../evil.pth", "/etc/x.py", "C:/x.py", "neonize\\..\\evil.py", "../neonize/x.py",
    "neonize/run.bat", "neonize/x.exe", "neonize/leia.txt", "neonize/data.json",
])
def test_o_que_nao_e_do_pacote_neonize_e_recusado(nome):
    assert problemas(nome)


def test_link_simbolico_e_recusado():
    assert any("symbolic" in p for p in problemas(links=["neonize/link.py"]))


def test_muitos_membros_sao_recusados(monkeypatch):
    monkeypatch.setattr(preparar, "MAX_MEMBERS", 2)
    assert any("members" in p for p in problemas("neonize/a.py", "neonize/b.py", "neonize/c.py"))


def test_extrai_so_dentro_da_pasta_de_destino(tmp_path):
    destino = tmp_path / "site-packages"
    destino.mkdir()
    preparar.extract_checked(zip_com("neonize/__init__.py", "neonize/proto/x.py"), str(destino))
    assert (destino / "neonize" / "__init__.py").read_bytes() == b"x"
    assert (destino / "neonize" / "proto" / "x.py").exists()
    assert not (tmp_path / "evil.pth").exists()


def test_arquivo_recusado_nao_extrai_nada(tmp_path):
    destino = tmp_path / "site-packages"
    destino.mkdir()
    with pytest.raises(SystemExit):
        preparar.extract_checked(zip_com("neonize/ok.py", "evil.pth"), str(destino))
    assert list(destino.iterdir()) == []


def test_o_tamanho_real_conta_mesmo_sem_a_checagem_do_cabecalho(tmp_path, monkeypatch):
    monkeypatch.setattr(preparar, "MAX_UNPACKED_BYTES", 10)
    monkeypatch.setattr(preparar, "check_members", lambda infos: [])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("neonize/grande.py", b"x" * 100)
    buf.seek(0)
    destino = tmp_path / "sp"
    destino.mkdir()
    with pytest.raises(SystemExit):
        preparar.extract_checked(zipfile.ZipFile(buf), str(destino))


def test_o_tamanho_declarado_acima_do_limite_e_recusado(monkeypatch):
    monkeypatch.setattr(preparar, "MAX_UNPACKED_BYTES", 10)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("neonize/grande.py", b"x" * 100)
    buf.seek(0)
    assert any("too large" in p for p in preparar.check_members(zipfile.ZipFile(buf).infolist()))


@pytest.mark.parametrize("url,esperado", [
    ("https://github.com/reffehcsoibaf/neonize/releases/download/x/y.zip", True),
    ("https://objects.githubusercontent.com/abc", True),
    ("http://github.com/x", False),
    ("https://evil.com/github.com", False),
    ("https://github.com.evil.com/x", False),
    ("https://notgithub.com/x", False),
])
def test_so_baixa_do_github(url, esperado):
    assert preparar.host_allowed(url) is esperado
