"""Em que pe esta o historico que o celular manda: o que history-sync-status
conta ao cliente.

O cliente usa isso para decidir se uma conversa curta e curta de verdade ou se
o resto do historico ainda esta a caminho (main_window/backfill.py
`refresh_history_still_landing`, `wait_for_restarted_history_sync`). Dizer
"completo" sempre fazia o primeiro sincronismo de uma conta recem-pareada dar
conversas curtas por definitivas. O neonize nao avisa o fim: a ponte conta os
blocos que entram em `_guardar_historico`, guarda o progresso (0-100) que cada
um traz e da o historico por completo quando o progresso chega a 100 ou quando
passa um periodo sem bloco novo.

Logica pura (o relogio entra por parametro), testada em test_historico_progresso.py.
"""
import threading

#: segundos sem bloco novo para dar o historico por completo
SILENCIO = 45
#: segundos, numa conta sem nada guardado, esperando o primeiro bloco
ESPERA_INICIAL = 120


class Progresso:
    def __init__(self, agora):
        self.criado = agora
        self.ultimo = None          # quando o ultimo bloco entrou ou saiu
        self.em_voo = 0             # blocos sendo guardados neste instante
        self.percentual = None      # o progresso do ultimo bloco que trouxe um
        self._trava = threading.Lock()

    def comecou(self, agora):
        with self._trava:
            self.em_voo += 1
            self.ultimo = agora

    def terminou(self, agora, percentual=None):
        with self._trava:
            self.em_voo = max(0, self.em_voo - 1)
            self.ultimo = agora
            if percentual is not None:
                self.percentual = int(percentual)

    def completo(self, agora, conversas_guardadas):
        with self._trava:
            if self.em_voo > 0:
                return False
            if self.ultimo is None:
                # nenhum bloco desde que a ponte subiu: conta antiga (ja tem
                # conversas) esta completa; conta nova espera o primeiro bloco
                return conversas_guardadas > 0 or agora - self.criado >= ESPERA_INICIAL
            if self.percentual is not None and self.percentual >= 100:
                return True
            return agora - self.ultimo >= SILENCIO

    def estado(self, agora, conversas_guardadas):
        """Os campos de history-sync-status que dependem do progresso."""
        completo = self.completo(agora, conversas_guardadas)
        return {"unprocessedChunks": self.em_voo,
                "initialSyncComplete": completo, "recentCompleted": completo}


def progresso_de(s, agora):
    """O Progresso da sessao (criado no primeiro uso)."""
    if getattr(s, "_progresso_historico", None) is None:
        s._progresso_historico = Progresso(agora)
    return s._progresso_historico
