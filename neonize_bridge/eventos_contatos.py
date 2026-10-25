"""Atualizacao de contatos da ponte: o evento `contacts.update` do Socket.IO, que
o cliente le em core/websocket_client.py (`on_contacts_update`) para manter o
nome (`pushName`) e a foto (`profilePicUrl`) dos contatos em dia sem uma
sincronizacao completa.

O que o cliente le de `data` (um dicionario ou uma lista deles), e so isto:
`remoteJid` (normalizado por ele; um `@lid` e ligado ao numero pelo mapa dele),
`pushName` (troca o nome guardado) e `profilePicUrl` (troca a URL guardada). Sem
nenhum dos dois o evento nao muda nada, e um `pushName` de alguem que ele nao
conhece cria o contato. Por isso um `@lid` sem numero conhecido nunca leva nome
daqui (criaria um contato com a chave do LID), e a foto so vai com a URL.

Fontes no neonize 0.5.2 (o que o descritor do proto confirma):
- `PictureEv` (JID, Author, Timestamp, Remove) so avisa que a foto mudou, nao
  traz a URL; ela vem de `get_profile_picture` numa thread, fora do manipulador
  (e uma chamada de rede). Foto removida nao e emitida: o cliente ignora URL vazia.
- O nome vem do `Info.Pushname` de cada mensagem recebida (`anunciar_nome`, que o
  manipulador de `MessageEv` do server.py chama: o neonize guarda um unico
  manipulador por evento, entao nao da para registrar outro aqui) e do pacote de
  historico. A build de teste do neonize pode ter eventos de contato (PushName,
  Contact); nenhum campo deles foi confirmado, entao nada os usa.

Cada fato so e emitido quando muda em relacao ao ultimo emitido (`Anunciados`,
em RAM): o armazem da ponte ja guarda os nomes e nao e reenviado. As funcoes
puras ficam no topo (testadas em test_eventos_contatos.py). Nada aqui foi provado
contra uma conta real. Nenhum JID ou nome vai para o log.
"""
import logging
import threading

import msgmap
from perfil import foto_para_wpp, sem_foto

log = logging.getLogger("ponte")

MAXIMO_DE_CONTATOS = 2000
# Fotos buscadas ao mesmo tempo (cada uma e uma chamada de rede).
BUSCAS_SIMULTANEAS = 4
_buscas = threading.BoundedSemaphore(BUSCAS_SIMULTANEAS)

SERVIDORES_DE_PESSOA = ("s.whatsapp.net", "c.us", "lid")


def e_pessoa(jid) -> bool:
    """O JID do neonize e de uma pessoa (nao grupo, canal nem lista)?"""
    return bool(getattr(jid, "User", "")) and getattr(jid, "Server", "") in SERVIDORES_DE_PESSOA


def nome_limpo(nome) -> str:
    """O apelido sem espacos nas pontas; '' se nao e texto ou esta vazio."""
    return nome.strip() if isinstance(nome, str) else ""


def contato_wpp(jid_wpp: str, nome: str = "", foto: str = "") -> dict:
    """Um contato do `data`: so leva `pushName` e `profilePicUrl` quando ha o que
    dizer (o cliente ignora campo vazio)."""
    contato = {"remoteJid": jid_wpp}
    if nome:
        contato["pushName"] = nome
    if foto:
        contato["profilePicUrl"] = foto
    return contato


def evento_de_contatos(sessao: str, contatos: list) -> dict:
    """O corpo do `contacts.update`: a sessao (o cliente descarta evento de outra)
    e a lista de contatos."""
    return {"session": sessao, "data": contatos}


class Anunciados:
    """O ultimo nome e a ultima foto emitidos de cada contato, para nao repetir.
    A foto e comparada pelo id dela (a URL tem validade e muda a cada pedido)."""

    def __init__(self):
        self._vistos = {}   # jid_wpp -> {"nome": str, "foto": str}

    def _mudou(self, jid_wpp, campo, valor) -> bool:
        if not valor:
            return False
        ficha = self._vistos.pop(jid_wpp, None) or {}   # pop+set: o mais novo fica no fim
        mudou = ficha.get(campo) != valor
        ficha[campo] = valor
        self._vistos[jid_wpp] = ficha
        while len(self._vistos) > MAXIMO_DE_CONTATOS:
            del self._vistos[next(iter(self._vistos))]
        return mudou

    def nome_mudou(self, jid_wpp, nome) -> bool:
        return self._mudou(jid_wpp, "nome", nome)

    def foto_mudou(self, jid_wpp, id_da_foto) -> bool:
        return self._mudou(jid_wpp, "foto", id_da_foto)


def _texto(jid) -> str:
    return f"{jid.User}@{jid.Server}" if getattr(jid, "User", "") else ""


def jid_para_o_app(c, jid) -> str:
    """JID do neonize -> o id que o app usa ('...@c.us'). Um `@lid` vira o numero
    quando o whatsmeow o conhece; senao continua `@lid` (o chamador decide)."""
    texto = _texto(jid)
    if getattr(jid, "Server", "") == "lid":
        try:
            numero = _texto(c.get_pn_from_lid(jid))
        except Exception:
            numero = ""
        if numero:
            texto = numero
    return msgmap.jid_wpp(texto)


def anunciar_nome(c, s, jid, nome) -> bool:
    """Emite `contacts.update` com o apelido de `jid` se ele mudou desde o ultimo
    emitido. Para o manipulador de MessageEv do server.py chamar com o remetente
    e `Info.Pushname` de uma mensagem de outra pessoa. True se emitiu."""
    nome = nome_limpo(nome)
    if not nome or not e_pessoa(jid):
        return False
    id_wpp = jid_para_o_app(c, jid)
    if id_wpp.endswith("@lid"):     # sem numero: criaria um contato com chave de LID
        return False
    if not s.contatos_anunciados.nome_mudou(id_wpp, nome):
        return False
    log.info("contato: apelido mudou")
    s.emitir("contacts.update", evento_de_contatos(
        s.nome, [contato_wpp(id_wpp, nome=nome)]))
    return True


def anunciar_foto(c, s, jid) -> bool:
    """Busca a foto atual de `jid` e emite `contacts.update` com a URL se o id da
    foto mudou desde o ultimo emitido. Chamada de rede: nunca no manipulador do
    evento. True se emitiu."""
    with _buscas:
        try:
            info = c.get_profile_picture(jid)
        except Exception as erro:
            if not sem_foto(erro):      # sem foto e resposta definitiva, nao falha
                log.info("foto de contato: %s", type(erro).__name__)
            return False
    foto = foto_para_wpp(info, "")
    if foto is None:
        return False
    id_wpp = jid_para_o_app(c, jid)
    if not s.contatos_anunciados.foto_mudou(id_wpp, foto["tag"] or foto["eurl"]):
        return False
    log.info("contato: foto mudou")
    s.emitir("contacts.update", evento_de_contatos(
        s.nome, [contato_wpp(id_wpp, foto=foto["eurl"])]))
    return True


def _em_segundo_plano(funcao, *args):
    threading.Thread(target=funcao, args=args, daemon=True, name="foto-contato").start()


def _anunciar_foto_sem_falhar(c, s, jid):
    try:
        anunciar_foto(c, s, jid)
    except Exception:
        log.exception("foto de contato")


def registrar_eventos(cli, s):
    """Liga o PictureEv do cliente neonize `cli` a sessao `s` (precisa de `s.nome`
    e `s.emitir`) e cria `s.contatos_anunciados`, que `anunciar_nome` usa. Chamado
    na thread do neonize."""
    import neonize.events as nev

    s.contatos_anunciados = Anunciados()

    @cli.event(nev.PictureEv)
    def _foto(c, e):
        try:
            if e.Remove or not e_pessoa(e.JID):
                return
            _em_segundo_plano(_anunciar_foto_sem_falhar, c, s, e.JID)
        except Exception:
            log.exception("foto mudou")
