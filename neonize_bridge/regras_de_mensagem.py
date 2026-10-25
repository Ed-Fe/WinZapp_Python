"""Regras das mensagens que a ponte decide antes de chamar o neonize: quando
uma edicao e recusada e quem assina o apagar-para-todos.

Edicao: o WhatsApp so aceita editar uma mensagem sua por cerca de 15 minutos. Recusar
aqui, antes de chamar o neonize, deixa o cliente saber com certeza que nada
saiu e desfazer o texto otimista (main_window/message_actions.py); uma falha
depois do envio fica "desconhecida" para ele.

Funcoes puras, testadas em test_regras_de_mensagem.py.
"""

import msgmap

#: janela de edicao do WhatsApp, em segundos
JANELA_DE_EDICAO = 15 * 60


def motivo_da_recusa(de_mim, original, agora):
    """'not_mine', 'not_stored' ou 'outside_window' se a edicao nao pode
    sair; '' se pode. `original` e a mensagem guardada (ou None)."""
    if not de_mim:
        return "not_mine"
    if original is None:
        return "not_stored"
    quando = original.get("t") or 0
    if quando and agora - quando > JANELA_DE_EDICAO:
        return "outside_window"
    return ""


def autor_da_revogacao(de_mim, grupo, eu, autor_do_id, original):
    """O JID (whatsmeow) que assina o 'apagar para todos' ('' se nao da para
    apagar). Mensagem minha: eu. Mensagem de outro so se apaga num grupo (o
    administrador revoga), e a revogacao leva o autor dela."""
    if de_mim:
        return eu
    if not grupo:
        return ""
    if autor_do_id:
        return autor_do_id
    return msgmap.jid_whatsmeow(original.get("author")) if original.get("author") else ""
