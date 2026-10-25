"""O que history-sync-status conta sobre o historico (historico_progresso.py)."""
from types import SimpleNamespace as N

import historico_progresso as hp


def test_conta_antiga_sem_blocos_novos_esta_completa():
    p = hp.Progresso(agora=0)
    assert p.completo(1, conversas_guardadas=40) is True


def test_conta_nova_espera_o_primeiro_bloco_e_depois_desiste():
    p = hp.Progresso(agora=0)
    assert p.completo(10, conversas_guardadas=0) is False
    assert p.completo(hp.ESPERA_INICIAL, conversas_guardadas=0) is True


def test_bloco_em_voo_nunca_e_completo_e_conta_como_nao_processado():
    p = hp.Progresso(agora=0)
    p.comecou(5)
    estado = p.estado(6, conversas_guardadas=10)
    assert estado == {"unprocessedChunks": 1, "initialSyncComplete": False,
                      "recentCompleted": False}


def test_progresso_abaixo_de_100_espera_o_silencio():
    p = hp.Progresso(agora=0)
    p.comecou(5)
    p.terminou(6, percentual=40)
    assert p.completo(7, conversas_guardadas=10) is False
    assert p.completo(6 + hp.SILENCIO - 1, conversas_guardadas=10) is False
    assert p.completo(6 + hp.SILENCIO, conversas_guardadas=10) is True


def test_progresso_100_completa_na_hora():
    p = hp.Progresso(agora=0)
    p.comecou(5)
    p.terminou(6, percentual=100)
    assert p.estado(7, conversas_guardadas=10)["recentCompleted"] is True


def test_novo_bloco_depois_de_completo_volta_a_esperar():
    p = hp.Progresso(agora=0)
    p.comecou(1)
    p.terminou(2, percentual=100)
    p.comecou(3)
    p.terminou(4, percentual=10)
    assert p.completo(5, conversas_guardadas=10) is False


def test_terminou_sem_comecou_nao_fica_negativo():
    p = hp.Progresso(agora=0)
    p.terminou(1)
    assert p.em_voo == 0


def test_progresso_de_cria_um_so_por_sessao():
    s = N()
    assert hp.progresso_de(s, 0) is hp.progresso_de(s, 99)
