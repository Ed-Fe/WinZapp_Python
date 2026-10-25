"""Rotas de canais: as funcoes puras que montam o que a ponte responde."""
import base64
from types import SimpleNamespace as N

import pytest

import canais


def jid_texto(jid):
    return "%s@%s" % (jid.User, jid.Server)


def meta(mudo=2, verificado=2):
    return N(ID=N(User="120363", Server="newsletter"),
             ThreadMeta=N(Name=N(Text="Noticias"), Description=N(Text="Resumo"),
                          InviteCode="abc", CreationTime=1700000000,
                          SubscriberCount=42, VerificationState=verificado),
             ViewerMeta=N(Mute=mudo))


def test_id_de_canal_aceita_com_e_sem_servidor():
    assert canais.id_de_canal("120363") == "120363@newsletter"
    assert canais.id_de_canal(" 120363@newsletter ") == "120363@newsletter"


@pytest.mark.parametrize("texto", ["", None, "@newsletter", "5511@c.us", "5511@g.us"])
def test_id_de_canal_recusa_o_que_nao_e_canal(texto):
    assert canais.id_de_canal(texto) == ""


def test_imagem_de_texto_decodifica_base64_e_data_url():
    cru = base64.b64encode(b"\x89PNGdados").decode()
    assert canais.imagem_de_texto(cru) == b"\x89PNGdados"
    assert canais.imagem_de_texto("data:image/png;base64," + cru) == b"\x89PNGdados"


def test_imagem_de_texto_vazia_e_bytes_vazios():
    assert canais.imagem_de_texto(None) == b""
    assert canais.imagem_de_texto("  ") == b""


@pytest.mark.parametrize("texto", ["/etc/passwd", "https://exemplo.com/a.png", "C:\\x.png"])
def test_imagem_que_nao_e_base64_e_recusada_em_vez_de_virar_caminho(texto):
    with pytest.raises(ValueError):
        canais.imagem_de_texto(texto)


def test_pedido_de_criacao_le_o_corpo_plano():
    foto = base64.b64encode(b"x").decode()
    assert canais.pedido_de_criacao(
        {"name": " Meu canal ", "description": "d", "picture": foto}
    ) == {"name": "Meu canal", "description": "d", "picture": b"x"}


def test_pedido_de_criacao_le_as_opcoes_do_wppconnect():
    foto = base64.b64encode(b"y").decode()
    pedido = canais.pedido_de_criacao(
        {"name": "C", "options": {"description": "da opcao", "picture": foto}})
    assert pedido == {"name": "C", "description": "da opcao", "picture": b"y"}


def test_pedido_de_criacao_sem_nome_e_recusado():
    with pytest.raises(ValueError):
        canais.pedido_de_criacao({"description": "x"})
    with pytest.raises(ValueError):
        canais.pedido_de_criacao(None)


def test_pedido_de_criacao_sem_descricao_nem_imagem():
    assert canais.pedido_de_criacao({"name": "C"}) == {
        "name": "C", "description": "", "picture": b""}


def test_silenciar_de_aceita_os_nomes_e_os_tipos():
    assert canais.silenciar_de({"mute": False}) is False
    assert canais.silenciar_de({"value": "false"}) is False
    assert canais.silenciar_de({"muted": True}) is True
    assert canais.silenciar_de({"value": "true"}) is True


def test_silenciar_de_sem_nada_silencia():
    assert canais.silenciar_de({}) is True
    assert canais.silenciar_de(None) is True


def test_canal_para_wpp_monta_a_resposta():
    assert canais.canal_para_wpp(meta(), jid_texto) == {
        "id": "120363@newsletter", "name": "Noticias", "description": "Resumo",
        "inviteCode": "abc", "creationTime": 1700000000, "subscribersCount": 42,
        "verified": False, "muted": False}


def test_canal_para_wpp_marca_mudo_e_verificado():
    resposta = canais.canal_para_wpp(meta(mudo=1, verificado=1), jid_texto)
    assert resposta["muted"] is True and resposta["verified"] is True


def test_canal_nao_achado_reconhece_o_erro_do_whatsapp():
    assert canais.canal_nao_achado(Exception("item-not-found"))
    assert canais.canal_nao_achado(Exception("newsletter not found"))
    assert not canais.canal_nao_achado(Exception("timeout"))
