"""As recusas no formato que o cliente do WinZapp reconhece (respostas.py)."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace as N

import respostas

RAIZ = Path(__file__).resolve().parent.parent


def _corpo(resposta):
    return json.loads(resposta.text)


def test_desconectada_e_o_404_do_middleware_do_servidor_real():
    resposta = respostas.desconectada()
    corpo = _corpo(resposta)
    # client/api_patches/src/middleware/statusConnection.ts: disconnected()
    assert resposta.status == 404
    assert corpo["status"] == "Disconnected" and corpo["response"] is None
    assert corpo["message"]


def test_desconectada_passa_pelo_leitor_do_cliente():
    """sending.py `_check_wa_connection_closed` e message_edit.response_not_sent
    leem o status 404 e o campo `status` (sem diferenciar maiusculas)."""
    resposta = respostas.desconectada()
    corpo = _corpo(resposta)
    assert resposta.status == 404 and str(corpo.get("status", "")).lower() == "disconnected"
    assert corpo.get("reason") != "probe_timeout"     # nao e o 404 "fraco"


def test_sem_conexao_so_responde_quando_nao_ha_conexao():
    conectada = N(cliente=object(), status="CONNECTED")
    assert respostas.sem_conexao(conectada) is None
    assert respostas.sem_conexao(N(cliente=None, status="CONNECTED")).status == 404
    assert respostas.sem_conexao(N(cliente=object(), status="CLOSED")).status == 404


def _leitor_do_cliente():
    """core/reply_failure.py do cliente, sem importar o resto dele."""
    caminho = RAIZ / "client" / "core" / "reply_failure.py"
    spec = importlib.util.spec_from_file_location("reply_failure_do_cliente", caminho)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def test_citacao_ausente_e_reconhecida_pelo_cliente_como_recusa_antes_do_envio():
    resposta = respostas.citacao_ausente()
    corpo = _corpo(resposta)
    falsa = N(status_code=resposta.status, json=lambda: corpo)
    assert _leitor_do_cliente().quote_missing_before_send(falsa) is True
    assert resposta.status == 500        # nunca 4xx: clientes antigos tirariam a citacao e reenviariam


def test_edicao_recusada_leva_o_texto_que_o_cliente_procura():
    resposta = respostas.edicao_recusada("outside_window")
    assert "Cannot edit this message" in resposta.text      # message_actions.py
    assert 400 <= resposta.status < 500
