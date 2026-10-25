"""Uma conversa, dois enderecos: o numero (@s.whatsapp.net) e o LID (@lid).

O armazem guarda a conversa com a chave exata que o evento trouxe, mas o
cliente prefere o @lid quando o conhece e nas rotas pode mandar qualquer um dos
dois. Sem juntar os dois, get-messages responde 200 [] para o outro endereco
(e o cliente so tenta outro endereco quando o status nao e 200), send-seen nao
acha a linha, e arquivar, fixar e silenciar criam uma linha vazia nova sem
mexer na real: o list-chats seguinte volta como estava e a sincronizacao de
duas vias do cliente desfaz a acao.

`Sessao` herda `AliasDeConversa` (server.py). A logica de escolha e a funcao
pura `chaves_da_conversa`, testada em test_conversas_alias.py; o mapeamento
numero <-> LID vem do armazem do neonize (nada foi testado contra uma conta real).
"""
import msgmap

SERVIDORES_COM_ALIAS = ("@s.whatsapp.net", "@lid")


def formas(jid):
    """O JID na forma do whatsmeow, mais a forma legada @c.us do numero."""
    base = msgmap.jid_whatsmeow(jid)
    if base.endswith("@s.whatsapp.net"):
        return [base, base[:-len("@s.whatsapp.net")] + "@c.us"]
    return [base]


def chaves_da_conversa(existe, jid, outro_endereco):
    """As chaves, no armazem, da conversa de `jid`: a dada primeiro, depois o
    outro endereco (numero <-> LID). `existe(chave)` diz se a linha existe;
    `outro_endereco(jid)` devolve o outro endereco ou '' se nao o sabe."""
    candidatos = formas(jid)
    base = candidatos[0]
    if base.endswith(SERVIDORES_COM_ALIAS):
        outro = outro_endereco(base)
        if outro:
            candidatos += formas(outro)
    return [c for c in dict.fromkeys(candidatos) if existe(c)]


def _texto(jid):
    return f"{jid.User}@{jid.Server}" if getattr(jid, "User", "") else ""


class AliasDeConversa:
    """Metodos de `Sessao` que resolvem os enderecos de uma conversa."""

    def _outro_endereco(self, jid):
        """O LID de um numero ou o numero de um LID, pelo neonize; '' se nao sabe."""
        if self.cliente is None or self.status != "CONNECTED":
            return ""
        from neonize.utils import build_jid
        usuario, _, servidor = jid.partition("@")
        try:
            alvo = build_jid(usuario, servidor)
            achado = _texto(self.cliente.get_pn_from_lid(alvo) if servidor == "lid"
                            else self.cliente.get_lid_from_pn(alvo))
            return msgmap.jid_whatsmeow(achado) if achado else ""
        except Exception:
            return ""

    def chaves_da_conversa(self, jid):
        """Todas as chaves guardadas da conversa (a dada primeiro); [] se ela
        ainda nao existe no armazem."""
        return chaves_da_conversa(self.bd.existe_conversa, jid, self._outro_endereco)

    def chat_existente(self, jid):
        """A chave com que a conversa esta guardada (a do primeiro endereco que
        existe), ou '' se nenhum existe. Bloqueia numa chamada do neonize:
        chame de uma thread."""
        chaves = self.chaves_da_conversa(jid)
        return chaves[0] if chaves else ""
