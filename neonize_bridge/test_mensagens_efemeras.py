"""Mensagens temporarias: a conversao do temporizador (funcoes puras) e a rota
com um cliente de mentira."""
import asyncio
import time
from datetime import timedelta
from types import SimpleNamespace as N

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import mensagens_efemeras as me

DIA, SEMANA, TRIMESTRE = 86400, 7 * 86400, 90 * 86400


def test_booleano_liga_por_sete_dias_e_desliga():
    assert me.segundos_do_timer(True) == SEMANA
    assert me.segundos_do_timer(False) == 0
    assert me.segundos_do_timer(None) == SEMANA
    assert me.segundos_do_timer("") == SEMANA
    assert me.segundos_do_timer("false") == 0
    assert me.segundos_do_timer("True") == SEMANA


def test_segundos_validos_em_numero_ou_texto():
    for segundos in (0, DIA, SEMANA, TRIMESTRE):
        assert me.segundos_do_timer(segundos) == segundos
        assert me.segundos_do_timer(str(segundos)) == segundos
    assert me.segundos_do_timer(float(DIA)) == DIA


def test_duration_vence_value():
    assert me.segundos_do_timer(True, DIA) == DIA
    assert me.segundos_do_timer(False, TRIMESTRE) == TRIMESTRE
    assert me.segundos_do_timer(True, "") == SEMANA


def test_duracao_fora_das_opcoes_do_whatsapp_e_recusada():
    for ruim in (1, 3600, -1, 1.5, "abc", [], {}):
        with pytest.raises(ValueError):
            me.segundos_do_timer(ruim)


def test_timer_vai_em_nanossegundos():
    timer, _ = me.argumentos_do_timer(DIA, 1_700_000_000.0)
    assert timer == 86_400_000_000_000
    assert me.argumentos_do_timer(0, 1.0)[0] == 0
    assert isinstance(timer, int)


def test_carimbo_vira_o_agora_em_milissegundos_pela_conta_do_neonize():
    # a conta do wrapper do neonize: int(time.time() + setting_ts.total_seconds() * 1000)
    agora = time.time()
    _, atraso = me.argumentos_do_timer(DIA, agora)
    assert isinstance(atraso, timedelta)
    carimbo = int(agora + atraso.total_seconds() * 1000)
    assert abs(carimbo - int(agora * 1000)) <= 1


def test_aplicar_timer_chama_o_neonize_com_inteiro():
    chamadas = []
    cliente = N(set_disappearing_timer=lambda *a: chamadas.append(a))
    me.aplicar_timer(cliente, "JID", SEMANA, 1_700_000_000.0)
    jid, timer, carimbo = chamadas[0]
    assert jid == "JID" and timer == SEMANA * 10 ** 9 and type(timer) is int
    assert isinstance(carimbo, timedelta)


def test_propriedade_efemera():
    assert me.e_propriedade_efemera(" Ephemeral ")
    assert not me.e_propriedade_efemera("announce")
    assert not me.e_propriedade_efemera(None)


def pedir(corpo, conectada=True, falha=False):
    chamadas = []

    def set_disappearing_timer(jid, timer, carimbo):
        if falha:
            raise RuntimeError("recusado")
        chamadas.append((jid, timer))
    s = N(cliente=N(set_disappearing_timer=set_disappearing_timer) if conectada else None,
          status="CONNECTED" if conectada else "CLOSED")

    async def em_thread(funcao, *args):
        return funcao(*args)

    async def rodar():
        app = web.Application()
        me.registrar(
            app.router, sessao=lambda req: s, ok=lambda **kw: web.json_response(kw),
            erro=lambda m, status=500: web.json_response(
                {"status": "error", "message": str(m)}, status=status),
            destino_de=lambda texto: texto, em_thread=em_thread, agora=lambda: 1.0)
        async with TestClient(TestServer(app)) as cliente:
            resp = await cliente.post("/api/t/temporary-messages", json=corpo)
            return resp.status, await resp.json()
    status, resposta = asyncio.run(rodar())
    return status, resposta, chamadas


def test_rota_liga_em_conversa_e_em_grupo():
    status, corpo, chamadas = pedir({"phone": "5511", "isGroup": False, "value": True})
    assert status == 200 and corpo["response"] == {"duration": SEMANA}
    assert chamadas == [("5511@s.whatsapp.net", SEMANA * 10 ** 9)]
    _, _, chamadas = pedir({"phone": ["123-456"], "isGroup": True, "value": False})
    assert chamadas == [("123-456@g.us", 0)]


def test_rota_recusa_pedido_ruim_e_sem_conexao():
    assert pedir({"value": True})[0] == 400
    assert pedir({"phone": "5511", "value": 5})[0] == 400
    status, corpo, chamadas = pedir({"phone": "5511"}, conectada=False)
    assert status == 400 and corpo["message"] == "Disconnected" and not chamadas


def test_rota_devolve_500_quando_o_whatsapp_recusa():
    assert pedir({"phone": "5511"}, falha=True)[0] == 500


def pedir_propriedade(corpo, falha=False):
    """group-property de grupos_extra, que delega `ephemeral` a este modulo."""
    import grupos_extra
    chamadas = []

    def set_disappearing_timer(jid, timer, carimbo):
        if falha:
            raise RuntimeError("recusado")
        chamadas.append((jid, timer))
    s = N(cliente=N(set_disappearing_timer=set_disappearing_timer), status="CONNECTED")

    async def rodar():
        app = web.Application()
        grupos_extra.registrar(
            app.router, sessao=lambda req: s, ok=lambda **kw: web.json_response(kw),
            jid_de_texto=lambda texto: texto, jid_texto=None, qr_data_url=None)
        async with TestClient(TestServer(app)) as cliente:
            resp = await cliente.post("/api/t/group-property", json=corpo)
            return resp.status, await resp.json()
    status, resposta = asyncio.run(rodar())
    return status, resposta, chamadas


def test_group_property_ephemeral_usa_o_mesmo_timer():
    status, _, chamadas = pedir_propriedade(
        {"groupId": "123-456", "property": "ephemeral", "value": DIA})
    assert status == 200 and chamadas == [("123-456@g.us", DIA * 10 ** 9)]
    _, _, chamadas = pedir_propriedade(
        {"groupId": "123-456@g.us", "property": "ephemeral", "value": False})
    assert chamadas == [("123-456@g.us", 0)]


def test_group_property_ephemeral_recusa_valor_e_grupo_ruins():
    assert pedir_propriedade({"groupId": "1", "property": "ephemeral", "value": 99})[0] == 400
    assert pedir_propriedade({"property": "ephemeral", "value": True})[0] == 400
    assert pedir_propriedade({"groupId": "1", "property": "ephemeral"}, falha=True)[0] == 500


def test_group_property_de_outra_propriedade_segue_recusada():
    status, _, _ = pedir_propriedade({"groupId": "1", "property": "nope", "value": True})
    assert status == 400
