"""Rotas de grupo extras: as funcoes puras que montam o que a ponte devolve."""
import base64
import io
from types import SimpleNamespace as N

import pytest
from PIL import Image

import grupos_extra as g


def jid(usuario="", servidor="s.whatsapp.net"):
    return N(User=usuario, Server=servidor)


def pessoa(fone="", lid="", jid_="", admin=False, dono=False, nome=""):
    return N(JID=jid(jid_), LID=jid(lid, "lid"), PhoneNumber=jid(fone),
             IsAdmin=admin, IsSuperAdmin=dono, DisplayName=nome)


def grupo(participantes, nome="Turma", anuncio=False, trava=False):
    return N(JID=jid("111-222", "g.us"), OwnerPN=jid("5511"), OwnerJID=jid(),
             GroupName=N(Name=nome), GroupTopic=N(Topic="desc"),
             GroupAnnounce=N(IsAnnounce=anuncio), GroupLocked=N(isLocked=trava),
             GroupCreated=1700000000, Participants=participantes)


def test_jid_do_grupo_completa_so_o_que_falta():
    assert g.jid_do_grupo("111-222") == "111-222@g.us"
    assert g.jid_do_grupo("111-222@g.us") == "111-222@g.us"
    assert g.jid_do_grupo(" 333@lid ") == "333@lid"


def test_grupos_do_corpo_aceita_groupid_phone_e_lista():
    assert g.grupos_do_corpo({"groupId": "1@g.us"}) == ["1@g.us"]
    assert g.grupos_do_corpo({"phone": "1"}) == ["1@g.us"]
    assert g.grupos_do_corpo({"phone": ["1@g.us", "2@g.us"]}) == ["1@g.us", "2@g.us"]
    assert g.grupos_do_corpo({"groupId": "", "phone": "3@g.us"}) == ["3@g.us"]
    assert g.grupos_do_corpo({}) == []


def test_como_booleano():
    assert g.como_booleano(None) is True
    assert g.como_booleano(None, False) is False
    assert g.como_booleano("false") is False
    assert g.como_booleano("True") is True
    assert g.como_booleano(False) is False
    assert g.como_booleano(0) is False
    assert g.como_booleano("") is True


def test_wid_usa_o_formato_do_whatsapp_web():
    assert g.wid_de("5511:12@s.whatsapp.net") == {
        "server": "c.us", "user": "5511", "_serialized": "5511@c.us"}
    assert g.wid_de("333@lid")["_serialized"] == "333@lid"


def test_participante_prefere_o_numero_e_leva_o_lid():
    linha = g.participante_para_wpp(pessoa(fone="5511", lid="333", admin=True, nome="Ana"))
    assert linha == {"id": {"server": "c.us", "user": "5511", "_serialized": "5511@c.us"},
                     "isAdmin": True, "isSuperAdmin": False,
                     "lid": {"server": "lid", "user": "333", "_serialized": "333@lid"},
                     "name": "Ana"}


def test_participante_so_com_lid_usa_o_lid_e_dono_conta_como_admin():
    linha = g.participante_para_wpp(pessoa(lid="333", dono=True))
    assert linha["id"]["_serialized"] == "333@lid"
    assert linha["isAdmin"] and linha["isSuperAdmin"]
    assert "name" not in linha


def test_membros_ids_e_administradores():
    info = grupo([pessoa(fone="1", admin=True), pessoa(fone="2"), pessoa(fone="3", dono=True)])
    assert [m["id"]["user"] for m in g.membros_para_wpp(info)] == ["1", "2", "3"]
    assert [i["_serialized"] for i in g.ids_dos_membros(info)] == ["1@c.us", "2@c.us", "3@c.us"]
    assert [i["_serialized"] for i in g.ids_dos_administradores(info)] == ["1@c.us", "3@c.us"]


def test_grupo_vira_chat_de_all_groups():
    chat = g.grupo_para_chat(grupo([pessoa(fone="1")], anuncio=True, trava=True))
    assert chat["id"]["_serialized"] == "111-222@g.us" and chat["isGroup"] is True
    meta = chat["groupMetadata"]
    assert meta["subject"] == "Turma" and meta["desc"] == "desc"
    assert meta["announce"] is True and meta["restrict"] is True
    assert meta["owner"]["_serialized"] == "5511@c.us"
    assert meta["creation"] == 1700000000
    assert len(meta["participants"]) == 1


def test_grupo_sem_dono_conhecido_nao_quebra():
    info = grupo([])
    info.OwnerPN = jid()
    assert g.grupo_para_chat(info)["groupMetadata"]["owner"] is None


def test_propriedades_que_o_neonize_sabe_mudar():
    assert g.acao_da_propriedade("announce") == "announce"
    assert g.acao_da_propriedade("Restrict") == "locked"
    assert g.acao_da_propriedade("ephemeral") is None
    assert g.acao_da_propriedade(None) is None


def png(lado=10):
    saida = io.BytesIO()
    Image.new("RGBA", (lado, lado), (255, 0, 0, 128)).save(saida, format="PNG")
    return saida.getvalue()


def test_imagem_do_corpo_le_base64_e_data_uri():
    dados = png()
    puro = base64.b64encode(dados).decode()
    assert g.imagem_do_corpo({"base64": puro}) == dados
    assert g.imagem_do_corpo({"path": "data:image/png;base64," + puro}) == dados
    assert g.imagem_do_corpo({}) == b""


def test_imagem_do_corpo_recusa_o_que_nao_e_base64_e_nao_le_arquivos():
    with pytest.raises(ValueError):
        g.imagem_do_corpo({"path": "C:/Users/x/foto.png"})
    with pytest.raises(ValueError):
        g.imagem_do_corpo({"base64": "isso nao e base64!"})


def test_foto_vira_jpeg_e_encolhe():
    jpeg = g.foto_para_jpeg(png(1000))
    imagem = Image.open(io.BytesIO(jpeg))
    assert imagem.format == "JPEG" and max(imagem.size) == g.LADO_MAXIMO_FOTO


def test_jpeg_pequeno_passa_sem_recompressao():
    saida = io.BytesIO()
    Image.new("RGB", (50, 50)).save(saida, format="JPEG")
    assert g.foto_para_jpeg(saida.getvalue()) == saida.getvalue()


def test_foto_invalida_levanta_erro():
    with pytest.raises(Exception):
        g.foto_para_jpeg(b"nao e imagem")
