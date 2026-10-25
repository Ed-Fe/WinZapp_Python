"""Duas rotas de mensagens que faltavam: `all-new-messages` (o que chegou desde
a chamada anterior) e `send-status` (publicar um status de texto).

`all-new-messages`: cada sessao tem um cursor na memoria da ponte, a posicao
(rowid) da ultima mensagem do armazem que ja foi entregue. A rota devolve as
mensagens guardadas depois dela, recebidas de outras pessoas (as minhas, o
mural de status e os avisos de "apagada" ficam de fora), e avanca o cursor. A
primeira chamada de uma sessao so marca o ponto de partida e responde vazio.
Limites, ditos de proposito:
- o cursor some quando a ponte reinicia (a primeira chamada depois recomeca);
- o historico que o celular manda ao parear tambem chega ao armazem, entao a
  chamada seguinte a ele o devolve inteiro (em fatias de LIMITE);
- o WPPConnect nao documenta o formato desta rota; a resposta e a lista simples
  de mensagens, como `all-unread-messages`.

`send-status`: o WPPConnect publica o texto de `message` em status@broadcast;
aqui sai como o `send-text-storie` (status de texto), pelo `publicar_status` do
server.py.

Nada aqui foi testado contra uma conta real. As funcoes puras ficam no topo
(testadas em test_conversas_novas.py); `registrar` liga as rotas ao servidor.
"""
import json

LIMITE = 5000        # mensagens por chamada; o resto vem na chamada seguinte


def ultimo_rowid(bd) -> int:
    """A posicao da mensagem mais recente guardada (0 se nao ha)."""
    with bd._trava:
        return int(bd._bd.execute("SELECT COALESCE(MAX(rowid), 0) FROM msgs").fetchone()[0])


def guardadas_depois(bd, rowid, limite=LIMITE):
    """[(rowid, mensagem no formato WPPConnect)] guardadas depois de `rowid`,
    da mais antiga a mais nova. Le o SQLite do armazem direto: o armazem nao
    tem esta consulta e store.py e de outra area."""
    with bd._trava:
        linhas = bd._bd.execute(
            "SELECT rowid, data FROM msgs WHERE rowid>? ORDER BY rowid LIMIT ?",
            (rowid, limite)).fetchall()
    return [(l["rowid"], json.loads(l["data"])) for l in linhas]


def e_nova_recebida(mensagem) -> bool:
    """Uma mensagem de outra pessoa que merece aparecer como nova."""
    if mensagem.get("fromMe") or mensagem.get("isStatus"):
        return False
    return mensagem.get("type") != "revoked"


def novas_e_cursor(linhas, cursor):
    """([mensagens novas], novo cursor) das `linhas` de guardadas_depois: o
    cursor passa por todas as linhas lidas, mesmo as que nao contam."""
    if not linhas:
        return [], cursor
    return [m for _, m in linhas if e_nova_recebida(m)], linhas[-1][0]


def texto_do_status(corpo) -> str:
    """O texto de `send-status` (campo `message`, ou `text`), ou ValueError."""
    texto = corpo.get("message") if isinstance(corpo, dict) else None
    if texto in (None, "") and isinstance(corpo, dict):
        texto = corpo.get("text")
    if not isinstance(texto, str) or not texto.strip():
        raise ValueError("message is required")
    return texto


def registrar(r, *, sessao, ok, erro, publicar_status, status_de_texto):
    """Liga as rotas ao roteador `r` do aiohttp. `publicar_status` e
    `status_de_texto` sao os do server.py (o mesmo caminho do send-text-storie)."""
    cursores = {}       # nome da sessao -> rowid da ultima mensagem entregue

    async def all_new_messages(req):
        s = sessao(req)
        cursor = cursores.get(s.nome)
        if cursor is None:
            cursores[s.nome] = ultimo_rowid(s.bd)
            return ok(status="success", response=[])
        novas, cursores[s.nome] = novas_e_cursor(guardadas_depois(s.bd, cursor), cursor)
        return ok(status="success", response=novas)

    async def send_status(req):
        try:
            corpo = await req.json()
            texto = texto_do_status(corpo)
        except ValueError as e:
            return erro(e, 400)
        return await publicar_status(req, lambda s, destino, _: status_de_texto(
            s, destino, {"text": texto, "options": corpo.get("options")}))

    r.add_get("/api/{tok}/all-new-messages", all_new_messages)
    r.add_post("/api/{tok}/send-status", send_status)
