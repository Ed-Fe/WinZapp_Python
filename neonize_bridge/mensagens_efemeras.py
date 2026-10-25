"""Mensagens temporarias (que somem sozinhas): a rota `temporary-messages` e o
ramo `ephemeral` de `group-property` (grupos_extra.py).

A unidade do temporizador, conferida no neonize 0.5.2 (client.py e o desmonte
da biblioteca nativa, nao so a documentacao):

- `set_disappearing_timer(jid, timer, setting_ts)` entrega `timer` inteiro,
  sem conta nenhuma, como `time.Duration` do Go: NANOSSEGUNDOS. O whatsmeow
  divide por 1e9 (`Duration.Seconds()`) para o `expiration` em segundos.
- Se `timer` for `timedelta`, o wrapper Python multiplica por 1000 (milissegundos,
  nao nanossegundos): o valor chegaria 1e6 vezes pequeno. Por isso a ponte
  SEMPRE passa um inteiro em nanossegundos.
- `setting_ts` e, no lado Go, milissegundos desde 1970 (`time.UnixMilli`);
  o wrapper calcula `int(time.time() + setting_ts.total_seconds() * 1000)`, entao
  `setting_ts` tem de ser o atraso que, somado a `time.time()`, da o agora em
  milissegundos. Sem `setting_ts` o Go recebe 1970.

O WhatsApp so oferece desligado, 24 horas, 7 dias e 90 dias; outros valores
sao recusados aqui. Nada disto foi testado contra uma conta real.

As funcoes puras ficam no topo (testadas em test_mensagens_efemeras.py);
`registrar` liga a rota ao servidor.
"""
import logging
from datetime import timedelta

from conversas_acoes import telefones
from conversas_listagem import booleano, jid_da_conversa

log = logging.getLogger("ponte")

#: duracoes aceitas, em segundos (0 = desligado)
DURACOES = (0, 24 * 3600, 7 * 24 * 3600, 90 * 24 * 3600)
#: `value: true` sem duracao: 7 dias (o padrao do WhatsApp Web ao ligar)
DURACAO_PADRAO = 7 * 24 * 3600
NANOS_POR_SEGUNDO = 10 ** 9


def segundos_do_timer(valor, duracao=None):
    """`value` (booleano ou segundos) e, se vier, `duration` (segundos) -> uma
    das DURACOES, ou ValueError. `duration` vence `value`; vazio liga."""
    if duracao not in (None, ""):
        valor = duracao
    if isinstance(valor, str):
        texto = valor.strip().lower()
        if texto in ("true", "false"):
            valor = texto == "true"
        elif texto.isdigit():
            valor = int(texto)
        elif texto:
            raise ValueError("value must be true, false or a number of seconds")
        else:
            valor = True
    if valor is None:
        valor = True
    if isinstance(valor, bool):
        return DURACAO_PADRAO if valor else 0
    if isinstance(valor, float) and valor.is_integer():
        valor = int(valor)
    if not isinstance(valor, int) or valor not in DURACOES:
        raise ValueError("duration must be one of %s seconds" % ", ".join(map(str, DURACOES)))
    return valor


def e_propriedade_efemera(propriedade) -> bool:
    return str(propriedade or "").strip().lower() == "ephemeral"


def argumentos_do_timer(segundos, agora):
    """(timer em nanossegundos, setting_ts) para `set_disappearing_timer`, ver o
    topo do modulo. `agora` e `time.time()`."""
    atraso = timedelta(seconds=(agora * 1000 - agora) / 1000)
    return int(segundos) * NANOS_POR_SEGUNDO, atraso


def aplicar_timer(cliente, jid, segundos, agora):
    """Liga, troca ou desliga o temporizador de uma conversa ou grupo."""
    timer, carimbo = argumentos_do_timer(segundos, agora)
    cliente.set_disappearing_timer(jid, timer, carimbo)


def registrar(r, *, sessao, ok, erro, destino_de, em_thread, agora):
    """Liga `temporary-messages` ao roteador `r` do aiohttp."""
    async def temporary_messages(req):
        s = sessao(req)
        try:
            corpo = await req.json()
            corpo = corpo if isinstance(corpo, dict) else {}
            fones = telefones(corpo)
            segundos = segundos_do_timer(corpo.get("value"), corpo.get("duration"))
        except ValueError as e:
            return erro(e, 400)
        if s.cliente is None or s.status != "CONNECTED":
            return erro("Disconnected", 400)
        grupo = booleano(corpo.get("isGroup"), False)
        try:
            for fone in fones:
                await em_thread(aplicar_timer, s.cliente,
                                destino_de(jid_da_conversa(fone, grupo)), segundos, agora())
        except Exception as e:
            log.exception("temporary-messages")
            return erro(e)
        return ok(status="success", response={"duration": segundos})

    r.add_post("/api/{tok}/temporary-messages", temporary_messages)
