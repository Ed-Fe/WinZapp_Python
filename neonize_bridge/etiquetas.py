"""Rotas de etiquetas (WhatsApp Business) da ponte: criar, apagar, aplicar a
conversas e listar. O WinZapp nao chama estas rotas; elas existem para quem
fala com a ponte como se fosse o WPPConnect Server.

O que o neonize permite: ESCREVER no estado de aplicativo (`send_app_state`
com um PatchInfo, o mesmo que o whatsmeow monta em BuildLabelEdit /
BuildLabelChat). O que ele NAO permite: LER. O neonize nao repassa os eventos
LabelEdit / LabelAssociationChat do whatsmeow, entao a ponte nao sabe quais
etiquetas ja existem na conta nem em que conversas estao. Por isso:

- `get-all-labels` lista so as etiquetas criadas por esta ponte (guardadas em
  `etiquetas.json` na pasta da sessao); as criadas no celular nao aparecem;
- as etiquetas da ponte usam ids a partir de 1000, para nunca sobrescrever uma
  etiqueta do celular (o celular numera 1, 2, 3...);
- `delete-all-labels` apaga so as etiquetas conhecidas da ponte.

As funcoes puras ficam no topo (testadas em test_etiquetas.py); `registrar`
liga as rotas ao servidor.
"""
import asyncio
import json
import logging
import os
import time

from aiohttp import web

log = logging.getLogger("ponte")

PRIMEIRO_ID_PROPRIO = 1000
CORES = 20                  # a paleta do WhatsApp tem 20 cores, indices 0..19
VERSAO_MUTACAO = 3          # a versao que o whatsmeow usa nestas mutacoes
INDICE_ETIQUETA = "label_edit"
INDICE_CONVERSA = "label_jid"
ARQUIVO = "etiquetas.json"


def carregar(pasta) -> dict:
    """{id: {"name", "colorIndex", "chats": [jid, ...]}}; vazio se nao ha arquivo."""
    try:
        with open(os.path.join(pasta, ARQUIVO), encoding="utf-8") as f:
            dados = json.load(f)
    except (OSError, ValueError):
        return {}
    return dados if isinstance(dados, dict) else {}


def guardar(pasta, etiquetas) -> None:
    caminho = os.path.join(pasta, ARQUIVO)
    with open(caminho + ".tmp", "w", encoding="utf-8") as f:
        json.dump(etiquetas, f, ensure_ascii=False)
    os.replace(caminho + ".tmp", caminho)


def proximo_id(etiquetas) -> str:
    usados = [int(i) for i in etiquetas if str(i).isdigit()]
    return str(max([PRIMEIRO_ID_PROPRIO - 1] + usados) + 1)


def indice_de_cor(opcoes, etiquetas) -> int:
    """Cor pedida em `options.labelColor` (ou `color`) quando e um indice
    valido da paleta; senao revezamos as cores pela quantidade de etiquetas.
    Valores ARGB / hexadecimais nao tem correspondencia segura com a paleta."""
    pedida = None
    if isinstance(opcoes, dict):
        pedida = opcoes.get("labelColor", opcoes.get("color"))
    if isinstance(pedida, int) and not isinstance(pedida, bool) and 0 <= pedida < CORES:
        return pedida
    return len(etiquetas) % CORES


def etiqueta_para_wpp(id_etiqueta, dados) -> dict:
    """Uma entrada de `get-all-labels`."""
    return {"id": str(id_etiqueta), "name": dados.get("name", ""),
            "colorIndex": int(dados.get("colorIndex", 0)),
            "count": len(dados.get("chats") or [])}


def lista_para_wpp(etiquetas) -> list:
    return [etiqueta_para_wpp(i, d) for i, d in sorted(
        etiquetas.items(), key=lambda par: int(par[0]) if str(par[0]).isdigit() else 0)]


def criar(etiquetas, nome, opcoes) -> dict:
    """Registra a etiqueta nova em `etiquetas` e devolve sua entrada."""
    id_etiqueta = proximo_id(etiquetas)
    etiquetas[id_etiqueta] = {"name": nome, "colorIndex": indice_de_cor(opcoes, etiquetas),
                              "chats": []}
    return etiqueta_para_wpp(id_etiqueta, etiquetas[id_etiqueta])


def aplicar(etiquetas, id_etiqueta, jid, colocar) -> None:
    """Marca / desmarca `jid` na etiqueta, se a ponte a conhece."""
    conversas = etiquetas.get(str(id_etiqueta), {}).get("chats")
    if conversas is None:
        return
    if colocar and jid not in conversas:
        conversas.append(jid)
    elif not colocar and jid in conversas:
        conversas.remove(jid)


def pedidos_de_etiqueta(opcoes) -> list:
    """`options` de add-or-remove-label ([{labelId, type: add|remove}]) ->
    [(id, colocar)]. Entrada fora do formato e ignorada."""
    saida = []
    for o in opcoes if isinstance(opcoes, list) else []:
        if not isinstance(o, dict) or o.get("labelId") in (None, ""):
            continue
        tipo = str(o.get("type", "add")).lower()
        if tipo in ("add", "remove"):
            saida.append((str(o["labelId"]), tipo == "add"))
    return saida


def mutacao_edicao(id_etiqueta, nome, cor, apagada, agora_ms) -> dict:
    """O que o whatsmeow chama BuildLabelEdit, como dicionario simples."""
    return {"indice": [INDICE_ETIQUETA, str(id_etiqueta)],
            "valor": {"timestamp": agora_ms,
                      "labelEditAction": {"name": nome, "color": cor, "deleted": apagada}}}


def mutacao_conversa(id_etiqueta, jid_texto_conversa, colocar, agora_ms) -> dict:
    """O que o whatsmeow chama BuildLabelChat, como dicionario simples."""
    return {"indice": [INDICE_CONVERSA, str(id_etiqueta), jid_texto_conversa],
            "valor": {"timestamp": agora_ms,
                      "labelAssociationAction": {"labeled": colocar}}}


def montar_patch(mutacoes):
    """Dicionarios de mutacao -> PatchInfo do neonize (importa o neonize aqui,
    para as funcoes acima serem testaveis sem ele)."""
    from neonize.proto.Neonize_pb2 import MutationInfo, PatchInfo
    from neonize.proto.waSyncAction.WAWebProtobufSyncAction_pb2 import (
        LabelAssociationAction, LabelEditAction, SyncActionValue)

    patch = PatchInfo(Timestamp=int(time.time() * 1000), Type=PatchInfo.REGULAR)
    for m in mutacoes:
        valor = m["valor"]
        if "labelEditAction" in valor:
            v = SyncActionValue(timestamp=valor["timestamp"],
                                labelEditAction=LabelEditAction(**valor["labelEditAction"]))
        else:
            v = SyncActionValue(
                timestamp=valor["timestamp"],
                labelAssociationAction=LabelAssociationAction(**valor["labelAssociationAction"]))
        patch.Mutations.append(MutationInfo(Index=m["indice"], Version=VERSAO_MUTACAO, Value=v))
    return patch


def registrar(r, *, sessao, ok, jid_de_texto, jid_texto):
    """Liga as rotas de etiquetas ao roteador `r` do aiohttp. Nenhuma foi
    testada contra uma conta real."""

    async def executar(funcao, *args):
        return await asyncio.get_running_loop().run_in_executor(None, funcao, *args)

    def erro(mensagem, status=500):
        return web.json_response({"status": "error", "message": str(mensagem)}, status=status)

    def conectada(s):
        return s.cliente is not None and s.status == "CONNECTED"

    # carregar -> enviar -> guardar nao pode se intercalar, ou dois pedidos
    # receberiam o mesmo id e um apagaria o registro do outro
    trava = asyncio.Lock()

    def agora_ms():
        return int(time.time() * 1000)

    async def enviar(s, mutacoes):
        await executar(s.cliente.send_app_state, montar_patch(mutacoes))

    async def add_new_label(req):
        s = sessao(req)
        corpo = await req.json()
        if not conectada(s):
            return erro("Disconnected", 400)
        nome = str(corpo.get("name") or "").strip()
        if not nome:
            return erro("name is required", 400)
        async with trava:
            etiquetas = carregar(s.pasta)
            nova = criar(etiquetas, nome, corpo.get("options"))
            try:
                await enviar(s, [mutacao_edicao(
                    nova["id"], nome, nova["colorIndex"], False, agora_ms())])
            except Exception as e:
                log.exception("add-new-label")
                return erro(e)
            guardar(s.pasta, etiquetas)
        return web.json_response({"status": "success", "response": nova}, status=201)

    async def add_or_remove_label(req):
        s = sessao(req)
        corpo = await req.json()
        if not conectada(s):
            return erro("Disconnected", 400)
        conversas = corpo.get("chatIds") or []
        pedidos = pedidos_de_etiqueta(corpo.get("options"))
        if not conversas or not pedidos:
            return erro("chatIds and options [{labelId, type}] are required", 400)
        jids = [jid_texto(jid_de_texto(c)) for c in conversas]
        agora = agora_ms()
        async with trava:
            try:
                await enviar(s, [mutacao_conversa(i, j, colocar, agora)
                                 for j in jids for i, colocar in pedidos])
            except Exception as e:
                log.exception("add-or-remove-label")
                return erro(e)
            etiquetas = carregar(s.pasta)
            for j in jids:
                for i, colocar in pedidos:
                    aplicar(etiquetas, i, j, colocar)
            guardar(s.pasta, etiquetas)
        return ok(status="success", response={"chatIds": list(conversas), "options": [
            {"labelId": i, "type": "add" if c else "remove"} for i, c in pedidos]})

    async def get_all_labels(req):
        s = sessao(req)
        return ok(status="success", response=lista_para_wpp(carregar(s.pasta)))

    async def apagar(s, ids):
        async with trava:
            etiquetas = carregar(s.pasta)
            agora = agora_ms()
            await enviar(s, [mutacao_edicao(
                i, etiquetas.get(i, {}).get("name", ""),
                etiquetas.get(i, {}).get("colorIndex", 0), True, agora) for i in ids])
            for i in ids:
                etiquetas.pop(i, None)
            guardar(s.pasta, etiquetas)

    async def delete_label(req):
        s = sessao(req)
        if not conectada(s):
            return erro("Disconnected", 400)
        id_etiqueta = req.match_info["id"]
        try:
            await apagar(s, [id_etiqueta])
        except Exception as e:
            log.exception("delete-label")
            return erro(e)
        return ok(status="success", response={"id": id_etiqueta})

    async def delete_all_labels(req):
        s = sessao(req)
        if not conectada(s):
            return erro("Disconnected", 400)
        try:
            async with trava:
                ids = list(carregar(s.pasta))
            if ids:
                await apagar(s, ids)
        except Exception as e:
            log.exception("delete-all-labels")
            return erro(e)
        return ok(status="success", response={"ids": ids})

    r.add_post("/api/{tok}/add-new-label", add_new_label)
    r.add_post("/api/{tok}/add-or-remove-label", add_or_remove_label)
    r.add_get("/api/{tok}/get-all-labels", get_all_labels)
    r.add_put("/api/{tok}/delete-all-labels", delete_all_labels)
    r.add_put("/api/{tok}/delete-label/{id}", delete_label)
