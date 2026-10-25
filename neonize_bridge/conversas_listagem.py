"""Rotas de leitura de conversas e mensagens da ponte: listas de conversas
(todas, arquivadas, com mensagens, silenciadas, listas de transmissao), uma
conversa pelo id, as mensagens guardadas de uma conversa, as nao lidas e os
votos de uma enquete. Tudo vem do armazem (store.py); nenhuma rota fala com o
WhatsApp.

O formato de cada conversa e o de `list-chats` (server.py), que e o que o
WinZapp le. O cliente nao chama estas rotas hoje; os formatos das que ele nao
usa seguem o que o WPPConnect devolve, de memoria, e nao foram conferidos
contra uma conta real.

As funcoes puras ficam no topo (testadas em test_conversas_listagem.py);
`registrar` liga as rotas ao servidor.
"""
import msgmap

TUDO = 1_000_000        # "todas" as mensagens: mais do que qualquer conversa guarda
SEM_FIM = 2**31 - 1     # silencio "para sempre" no armazem


def booleano(texto, padrao):
    """'true'/'1'/'yes' -> True, 'false'/'0'/'no' -> False, vazio -> padrao."""
    if texto is None or str(texto).strip() == "":
        return padrao
    return str(texto).strip().lower() in ("true", "1", "yes", "y")


def jid_da_conversa(texto, grupo=False):
    """Id recebido na rota -> JID do whatsmeow. Sem '@': numero, ou grupo se
    `grupo`."""
    texto = str(texto or "").strip()
    if "@" not in texto and grupo:
        texto += "@g.us"
    return msgmap.jid_whatsmeow(texto)


def nomes_dos_contatos(contatos):
    """{jid no formato WPPConnect: nome ou apelido} dos contatos guardados."""
    return {msgmap.jid_wpp(c["jid"]): c["name"] or c["pushname"] for c in contatos}


def conversa_para_wpp(c, nomes, id_wpp, mensagens=None):
    """Uma linha do armazem -> a conversa como `list-chats` a devolve."""
    jid = msgmap.jid_wpp(c["jid"])
    return {
        "id": id_wpp(c["jid"]),
        "name": c["name"] or nomes.get(jid) or None,
        "isGroup": jid.endswith("@g.us"),
        "unreadCount": c["unread"], "t": c["ts"],
        "archive": bool(c["archived"]), "pin": c["pinned"] or False,
        "muteExpiration": c["muted"], "isLocked": bool(c.get("locked")),
        "msgs": mensagens,
    }


def e_transmissao(jid):
    """Lista de transmissao (nao o mural de status, que nunca e conversa)."""
    return jid.endswith("@broadcast") and jid != "status@broadcast"


def silenciada(c, agora):
    return int(c["muted"] or 0) > agora


def selecionar(conversas, criterio, agora=0):
    """As conversas que atendem ao criterio: 'todas', 'arquivadas', 'transmissao'
    ou 'silenciadas'. O mural de status fica sempre de fora."""
    sem_status = [c for c in conversas if c["jid"] != "status@broadcast"]
    if criterio == "arquivadas":
        return [c for c in sem_status if c["archived"]]
    if criterio == "transmissao":
        return [c for c in sem_status if e_transmissao(c["jid"])]
    if criterio == "silenciadas":
        return [c for c in sem_status if silenciada(c, agora)]
    return sem_status


def do_tipo(conversas, tipo):
    """Filtro de `list-mutes/:type`: 'all', 'group' (ou 'groups') e 'user'
    (ou 'contact'). Outro tipo (por exemplo 'community') nao se reconhece
    pelo JID: devolve vazio."""
    tipo = str(tipo or "all").lower()
    if tipo == "all":
        return list(conversas)
    if tipo in ("group", "groups"):
        return [c for c in conversas if c["jid"].endswith("@g.us")]
    if tipo in ("user", "users", "contact", "contacts"):
        return [c for c in conversas
                if c["jid"].endswith(("@s.whatsapp.net", "@c.us", "@lid"))]
    return []


def sem_minhas(mensagens, incluir_minhas):
    return list(mensagens) if incluir_minhas else [m for m in mensagens if not m.get("fromMe")]


def nao_lidas_recebidas(mensagens, quantidade):
    """As ultimas `quantidade` mensagens recebidas (nao minhas), em ordem
    cronologica: as que o contador de nao lidas da conversa conta."""
    if quantidade <= 0:
        return []
    recebidas = [m for m in mensagens if not m.get("fromMe")]
    return recebidas[-quantidade:]


def registrar(r, *, sessao, ok, erro, id_wpp, agora):
    """Liga as rotas de leitura de conversas ao roteador `r` do aiohttp."""

    def chats(s, criterio, com_mensagens=False):
        nomes = nomes_dos_contatos(s.bd.contatos())
        saida = []
        for c in selecionar(s.bd.conversas(), criterio, agora()):
            ultimas = s.bd.mensagens(c["jid"], 1)
            if com_mensagens and not ultimas:
                continue
            saida.append(conversa_para_wpp(c, nomes, id_wpp, ultimas if com_mensagens else None))
        return saida

    async def all_chats(req):
        return ok(status="success", response=chats(sessao(req), "todas"))

    async def all_chats_archived(req):
        return ok(status="success", response=chats(sessao(req), "arquivadas"))

    async def all_chats_with_messages(req):
        s = sessao(req)
        lista = chats(s, "todas", com_mensagens=True)
        if booleano(req.query.get("withNewMessageOnly"), False):
            lista = [c for c in lista if c["unreadCount"] > 0]
        return ok(status="success", response=lista)

    async def all_broadcast_list(req):
        return ok(status="success", response=chats(sessao(req), "transmissao"))

    async def list_mutes(req):
        s = sessao(req)
        nomes = nomes_dos_contatos(s.bd.contatos())
        mudas = do_tipo(selecionar(s.bd.conversas(), "silenciadas", agora()),
                        req.match_info.get("type"))
        return ok(status="success", response=[conversa_para_wpp(c, nomes, id_wpp) for c in mudas])

    async def chat_by_id(req):
        s = sessao(req)
        jid = jid_da_conversa(req.match_info["phone"], booleano(req.query.get("isGroup"), False))
        achada = [c for c in s.bd.conversas() if c["jid"] == jid]
        if not achada:
            return erro("chat not found", 404)
        nomes = nomes_dos_contatos(s.bd.contatos())
        return ok(status="success", response=conversa_para_wpp(achada[0], nomes, id_wpp))

    async def mensagens_da_conversa(req):
        s = sessao(req)
        jid = jid_da_conversa(req.match_info["phone"], booleano(req.query.get("isGroup"), False))
        itens = s.bd.mensagens(jid, TUDO)
        return ok(status="success",
                  response=sem_minhas(itens, booleano(req.query.get("includeMe"), True)))

    def nao_lidas_por_conversa(s):
        """[(conversa, [mensagens recebidas nao lidas])] das conversas com nao lidas."""
        achadas = []
        for c in s.bd.conversas():
            if int(c["unread"] or 0) > 0 and c["jid"] != "status@broadcast":
                recentes = s.bd.mensagens(c["jid"], int(c["unread"]) * 3 + 50)
                achadas.append((c, nao_lidas_recebidas(recentes, int(c["unread"]))))
        return achadas

    async def unread_messages(req):
        s = sessao(req)
        nomes = nomes_dos_contatos(s.bd.contatos())
        return ok(status="success", response=[
            {**conversa_para_wpp(c, nomes, id_wpp), "messages": itens}
            for c, itens in nao_lidas_por_conversa(s)])

    async def all_unread_messages(req):
        s = sessao(req)
        return ok(status="success", response=[
            m for _, itens in nao_lidas_por_conversa(s) for m in itens])

    async def votes(req):
        s = sessao(req)
        votos = s.votos_wpp(req.match_info["id"])
        if votos is None:
            return erro("poll not found", 404)
        return ok(status="success", response=votos)

    p = "/api/{tok}/"
    r.add_get(p + "all-chats", all_chats)
    r.add_get(p + "all-chats-archived", all_chats_archived)
    r.add_get(p + "all-chats-with-messages", all_chats_with_messages)
    r.add_get(p + "all-broadcast-list", all_broadcast_list)
    r.add_get(p + "list-mutes/{type}", list_mutes)
    r.add_get(p + "chat-by-id/{phone}", chat_by_id)
    # O neonize nao pede historico antigo ao celular: "carregar" e ler o que
    # a ponte ja guardou, o mesmo que all-messages-in-chat.
    r.add_get(p + "all-messages-in-chat/{phone}", mensagens_da_conversa)
    r.add_get(p + "load-messages-in-chat/{phone}", mensagens_da_conversa)
    r.add_get(p + "unread-messages", unread_messages)
    r.add_get(p + "all-unread-messages", all_unread_messages)
    r.add_get(p + "votes/{id}", votes)
