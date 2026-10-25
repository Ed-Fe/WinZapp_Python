"""Recibos de grupo: o tique de uma mensagem nossa so avanca quando TODOS os
destinatarios chegaram la.

Antes, o primeiro recibo de qualquer membro virava `onack` 2/3/4 e o app marcava
a mensagem como "lida" assim que UMA pessoa a lia. Aqui o ack de um grupo e:

- 2 (entregue): todos os destinatarios mandaram algum recibo;
- 3 (lida): todos mandaram recibo de leitura ou de reproducao;
- 4 (ouvida): todos mandaram recibo de reproducao;
- 1 (enviada) enquanto isso. Conversa de uma pessoa so nao passa por aqui.

Destinatarios = participantes do grupo menos a propria conta, e so os que ja
estavam no grupo quando a mensagem saiu (`Membro.desde`, conhecido so para quem
entrou com a ponte ouvindo; quem veio de GetGroupInfo conta sempre). Cada pessoa
pode ter dois enderecos (numero e LID, e o numero brasileiro com e sem o 9o
digito): dois enderecos que se cruzam sao a mesma pessoa. Recibo de quem nao e
destinatario (saiu, entrou depois, e eu mesmo) e ignorado.

PALPITE, nao verificado com uma conta real: quem desligou a confirmacao de
leitura manda "entregue" mas nunca "lida". Se todos esperassem por ele, a
mensagem nunca ficaria azul. Por isso quem entregou e passou `graca` segundos
sem ler deixa de segurar o ack 3 (desde que pelo menos uma pessoa tenha lido de
verdade). O ack 4 nao tem essa folga: voz so fica "ouvida" quando todos ouviram.
Nao ha temporizador: a folga e reavaliada a cada recibo novo, em message-ack e
em get-messages.

As funcoes puras ficam no topo (test_recibos_de_grupo.py); `RecibosDeGrupo` e a
parte de `Sessao` que guarda a lista de membros e fala com o neonize.
"""
import collections
import logging
import threading
import time
from dataclasses import dataclass, field

import msgmap
from grupos import formas_do_numero
from redacao import redigir

log = logging.getLogger("ponte")

#: segundos que quem so entregou (sem ler) deixa de segurar o ack 3. Um palpite.
GRACA_SEM_LEITURA = 600
#: depois disto a lista de membros em cache e buscada de novo (GetGroupInfo)
VALIDADE_DO_ELENCO = 6 * 3600
#: quantas mensagens lembram o maior ack ja emitido
LIMITE_DE_ACKS = 5000

ETAPAS = ("delivered", "read", "played")


# ----------------------------------------------------------------- enderecos
def _normalizar(texto):
    """'x:12@c.us' -> 'x@s.whatsapp.net'; '' se nao e um endereco."""
    usuario, arroba, servidor = str(texto or "").partition("@")
    usuario = usuario.split(":")[0]
    if not usuario or not arroba or not servidor:
        return ""
    if servidor == "c.us":
        servidor = "s.whatsapp.net"
    return f"{usuario}@{servidor}"


def chaves_de(*enderecos):
    """Todas as formas comparaveis dos enderecos de UMA pessoa: sem dispositivo,
    @c.us igual a @s.whatsapp.net e, num celular brasileiro, com e sem o 9o digito."""
    chaves = set()
    for endereco in enderecos:
        base = _normalizar(endereco)
        if not base:
            continue
        usuario, _, servidor = base.partition("@")
        if servidor == "s.whatsapp.net" and usuario.isdigit():
            chaves.update(f"{forma}@{servidor}" for forma in formas_do_numero(usuario))
        else:
            chaves.add(base)
    return frozenset(chaves)


@dataclass(frozen=True)
class Membro:
    """Um participante de grupo: `id` (numero @c.us quando se sabe, senao o
    LID) e todas as `chaves` pelas quais ele pode aparecer num recibo."""
    id: str
    chaves: frozenset = field(compare=False)
    desde: int = 0      # segundos em que entrou, vistos ao vivo; 0 = nao se sabe


def _id_preferido(fone, jid, lid):
    escolhido = fone or (jid if jid and not jid.endswith("@lid") else "") or lid or jid
    return msgmap.jid_wpp(escolhido)


def membro_de_participante(p, texto):
    """Participante do `GroupInfo` do neonize -> Membro (ou None sem endereco).
    `texto(jid)` devolve 'usuario@servidor'."""
    fone = texto(getattr(p, "PhoneNumber", None))
    jid = texto(getattr(p, "JID", None))
    lid = texto(getattr(p, "LID", None))
    chaves = chaves_de(fone, jid, lid)
    if not chaves:
        return None
    return Membro(_id_preferido(fone, jid, lid), chaves)


def membro_de_endereco(endereco, outro="", desde=0):
    """Membro a partir de um endereco (de um evento de grupo) e, se sabido, do
    outro endereco da mesma pessoa."""
    chaves = chaves_de(endereco, outro)
    if not chaves:
        return None
    enderecos = [e for e in (endereco, outro) if e]
    fone = next((e for e in enderecos if not e.endswith("@lid")), "")
    lid = next((e for e in enderecos if e.endswith("@lid")), "")
    return Membro(_id_preferido(fone, "", lid), chaves, int(desde or 0))


# --------------------------------------------------------------- lista em cache
class Elenco:
    """Os membros de cada grupo, guardados entre mensagens. Seguro entre threads."""

    def __init__(self, validade=VALIDADE_DO_ELENCO):
        self.validade = validade
        self._grupos = {}       # grupo -> (membros, instante)
        self._trava = threading.RLock()

    def definir(self, grupo, membros, agora):
        """Troca a lista inteira (GetGroupInfo). Quem ja era conhecido guarda o
        `desde` que tinha: a lista nova nao sabe quando ninguem entrou."""
        with self._trava:
            antigos = self._grupos.get(grupo, ([], 0))[0]
            novos = []
            for m in membros:
                if not m.desde:
                    anterior = next((a for a in antigos if a.desde and a.chaves & m.chaves), None)
                    if anterior:
                        m = Membro(m.id, m.chaves, anterior.desde)
                novos.append(m)
            self._grupos[grupo] = (novos, agora)

    def membros(self, grupo):
        """A lista (mesmo velha) ou None se o grupo nunca foi carregado."""
        with self._trava:
            achado = self._grupos.get(grupo)
            return list(achado[0]) if achado else None

    def velho(self, grupo, agora):
        with self._trava:
            achado = self._grupos.get(grupo)
            return achado is None or agora - achado[1] >= self.validade

    def entrou(self, grupo, membro):
        """Alguem entrou. Grupo ainda nao carregado: nada a fazer (a carga
        inteira, depois, ja o traz)."""
        with self._trava:
            achado = self._grupos.get(grupo)
            if achado is None or membro is None:
                return
            membros = [m for m in achado[0] if not (m.chaves & membro.chaves)]
            membros.append(membro)
            self._grupos[grupo] = (membros, achado[1])

    def saiu(self, grupo, chaves):
        with self._trava:
            achado = self._grupos.get(grupo)
            if achado is None or not chaves:
                return
            self._grupos[grupo] = ([m for m in achado[0] if not (m.chaves & chaves)], achado[1])

    def invalidar(self, grupo):
        with self._trava:
            self._grupos.pop(grupo, None)


# ------------------------------------------------------------------ avaliacao
Resultado = collections.namedtuple("Resultado", "ack esperados entregues lidos ouvidos linhas")


def destinatarios(membros, minhas_chaves, instante_da_mensagem=0):
    """Quem deveria receber uma mensagem nossa: os membros menos a minha conta
    e menos quem entrou depois dela."""
    return [m for m in membros
            if not (m.chaves & minhas_chaves)
            and not (instante_da_mensagem and m.desde and m.desde > instante_da_mensagem)]


def _achar(indice, quem, resolver, memoria):
    chaves = chaves_de(quem)
    for c in chaves:
        if c in indice:
            return indice[c]
    if resolver is None:
        return None
    if quem not in memoria:
        try:
            memoria[quem] = resolver(quem) or ""
        except Exception:
            memoria[quem] = ""
    for c in chaves_de(memoria[quem]):
        if c in indice:
            return indice[c]
    return None


def avaliar(esperados, recibos, agora, graca=GRACA_SEM_LEITURA, resolver=None):
    """Recibos [(quem, etapa, ts)] de uma mensagem nossa num grupo -> Resultado.

    `esperados` vem de `destinatarios`; `resolver(endereco)` (opcional) devolve
    o outro endereco da pessoa quando os recibos e a lista nao se cruzam.
    `ack` e 1..4 conforme o modulo (1 tambem sem destinatarios). `linhas` sao
    so os recibos de destinatarios, com `quem` trocado pelo `id` do membro:
    uma linha por pessoa mesmo que ela tenha respondido por dois enderecos."""
    indice = {}
    for i, m in enumerate(esperados):
        for c in m.chaves:
            indice.setdefault(c, i)
    por_pessoa = [dict() for _ in esperados]    # etapa -> menor ts
    linhas, memoria = [], {}
    for quem, etapa, ts in recibos:
        if etapa not in ETAPAS:
            continue
        i = _achar(indice, quem, resolver, memoria)
        if i is None:
            continue
        ts = int(ts or 0)
        visto = por_pessoa[i].get(etapa)
        if visto is None or (ts and (not visto or ts < visto)):
            por_pessoa[i][etapa] = ts
        linhas.append((esperados[i].id, etapa, ts))

    entregues = lidos = ouvidos = sem_leitura = 0
    for etapas in por_pessoa:
        if not etapas:
            continue
        entregues += 1
        if "read" in etapas or "played" in etapas:
            lidos += 1
        else:
            horas = [t for t in etapas.values() if t]
            if horas and agora - min(horas) >= graca:
                sem_leitura += 1
        if "played" in etapas:
            ouvidos += 1

    n = len(esperados)
    ack = 1
    if n and entregues == n:
        ack = 2
        if lidos >= 1 and lidos + sem_leitura == n:
            ack = 3
        if ouvidos == n:
            ack = 4
    return Resultado(ack, n, entregues, lidos, ouvidos, linhas)


# ------------------------------------------------------------ eventos de grupo
def aplicar_evento(elenco, grupo, e, agora, texto, outro_endereco=None):
    """GroupInfoEv -> entradas e saidas na lista de `grupo`. `texto(jid)` ->
    'usuario@servidor'; `outro_endereco(endereco)` -> o outro endereco ou ''."""
    desde = agora
    try:
        desde = msgmap.segundos(e.Timestamp) or agora
    except Exception:
        pass

    def par(endereco):
        try:
            return (outro_endereco(endereco) or "") if outro_endereco else ""
        except Exception:
            return ""

    for jid in getattr(e, "Leave", ()):
        endereco = texto(jid)
        elenco.saiu(grupo, chaves_de(endereco, par(endereco)))
    for jid in getattr(e, "Join", ()):
        endereco = texto(jid)
        elenco.entrou(grupo, membro_de_endereco(endereco, par(endereco), desde))


# ---------------------------------------------------------------------- sessao
def _texto(jid):
    if jid is None or not getattr(jid, "User", ""):
        return ""
    return f"{jid.User}@{jid.Server}"


class _Estado:
    def __init__(self):
        self.elenco = Elenco()
        self.acks = collections.OrderedDict()   # id da mensagem -> maior ack emitido
        self.trava = threading.Lock()


class RecibosDeGrupo:
    """Metodos de `Sessao` para o ack de mensagens de grupo (usa self.bd,
    self.cliente, self.status, self.eu, self.lid e _outro_endereco)."""

    def _est_recibos(self):
        estado = self.__dict__.get("_estado_de_recibos")
        if estado is None:
            estado = self.__dict__.setdefault("_estado_de_recibos", _Estado())
        return estado

    def _minhas_chaves(self):
        return chaves_de(self.eu, self.lid)

    def _resolver_endereco(self, endereco):
        return self._outro_endereco(msgmap.jid_whatsmeow(endereco))

    # --- lista de membros
    def elenco_registrar(self, info, agora=None):
        """`GroupInfo` do neonize (get_joined_groups, get_group_info, JoinedGroupEv)."""
        grupo = msgmap.jid_whatsmeow(_texto(getattr(info, "JID", None)))
        if not grupo.endswith("@g.us"):
            return
        membros = [m for m in (membro_de_participante(p, _texto)
                               for p in getattr(info, "Participants", ())) if m]
        if membros:
            self._est_recibos().elenco.definir(grupo, membros, agora or time.time())

    def elenco_mudou(self, e, agora=None):
        """GroupInfoEv: quem entrou e quem saiu."""
        grupo = msgmap.jid_whatsmeow(_texto(getattr(e, "JID", None)))
        if grupo.endswith("@g.us"):
            aplicar_evento(self._est_recibos().elenco, grupo, e, agora or time.time(),
                           _texto, self._resolver_endereco)

    def membros_do_grupo(self, chat, buscar=True, agora=None):
        """Os membros do grupo, ou None se ainda nao se sabe. `buscar` pede o
        GetGroupInfo ao neonize quando falta ou venceu (bloqueia: chame de
        thread); falhando, vale a lista velha."""
        grupo = msgmap.jid_whatsmeow(chat)
        elenco = self._est_recibos().elenco
        agora = agora or time.time()
        if buscar and elenco.velho(grupo, agora) and self.cliente is not None \
                and self.status == "CONNECTED":
            try:
                from neonize.utils import build_jid
                usuario, _, servidor = grupo.partition("@")
                self.elenco_registrar(self.cliente.get_group_info(build_jid(usuario, servidor)), agora)
            except Exception as erro:
                log.info("membros do grupo %s: %s", redigir(grupo), type(erro).__name__)
        return elenco.membros(grupo)

    # --- avaliacao
    def _instante_da_mensagem(self, mid):
        try:
            return int((self.bd.mensagem_wpp(mid) or {}).get("t") or 0)
        except (TypeError, ValueError):
            return 0

    def _avaliar_grupo(self, chat, mid, buscar, agora, graca):
        """Resultado de `avaliar` para a mensagem, ou None se nao se sabe quem
        sao os destinatarios (lista de membros ou conta propria desconhecidas)."""
        minhas = self._minhas_chaves()
        membros = self.membros_do_grupo(chat, buscar, agora) if minhas else None
        if not membros:
            return None
        esperados = destinatarios(membros, minhas, self._instante_da_mensagem(mid))
        return avaliar(esperados, self.bd.recibos_de(mid), agora, graca,
                       self._resolver_endereco if buscar else None)

    def resumo_de_recibos(self, chat, mid, agora=None, graca=GRACA_SEM_LEITURA):
        """Para message-ack: (recibos so dos destinatarios, quantos eram) ou
        None se nao se sabe. Pode chamar o neonize: chame de thread."""
        r = self._avaliar_grupo(chat, mid, True, agora or time.time(), graca)
        return None if r is None else (r.linhas, r.esperados)

    def _lembrar_ack(self, mid, ack):
        """Guarda o maior ack ja visto; True se `ack` o superou."""
        estado = self._est_recibos()
        with estado.trava:
            if ack <= estado.acks.get(mid, 0):
                return False
            estado.acks[mid] = ack
            estado.acks.move_to_end(mid)
            while len(estado.acks) > LIMITE_DE_ACKS:
                estado.acks.popitem(last=False)
            return True

    def ack_de_grupo_a_emitir(self, chat, mid, agora=None, graca=GRACA_SEM_LEITURA):
        """O `onack` a emitir agora para uma mensagem nossa de grupo: 2, 3 ou 4,
        ou None (nada novo, ou nao se sabe quem sao os destinatarios). Nunca
        devolve um ack menor ou igual a um ja emitido."""
        r = self._avaliar_grupo(chat, mid, True, agora or time.time(), graca)
        if r is None:
            log.info("ack de grupo %s: membros desconhecidos", redigir(chat))
            return None
        log.info("ack de grupo %s: %d/%d entregue, %d lida, %d ouvida -> %d",
                 redigir(chat), r.entregues, r.esperados, r.lidos, r.ouvidos, r.ack)
        if r.ack >= 2 and self._lembrar_ack(mid, r.ack):
            return r.ack
        return None

    def ack_da_mensagem(self, w, agora=None, graca=GRACA_SEM_LEITURA):
        """O `ack` de uma mensagem nossa para get-messages/message-by-id: None
        fora de grupo (vale o dos recibos); em grupo, so le o cache (nao
        bloqueia o laco) e nunca fica abaixo do maior ack ja emitido."""
        chat = (w.get("id") or {}).get("remote") or ""
        if not str(chat).endswith("@g.us"):
            return None
        mid = w["id"]["id"]
        r = self._avaliar_grupo(chat, mid, False, agora or time.time(), graca)
        ack = r.ack if r is not None else 1
        return max(ack, self._est_recibos().acks.get(mid, 1))
