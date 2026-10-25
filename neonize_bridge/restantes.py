"""Rotas que sobraram depois das areas: o que o neonize 0.5.2 nao consegue (501,
com o motivo), os termos da Meta AI e a plataforma de uma mensagem.

O que foi verificado no pacote neonize 0.5.2 (client.py, _binder.py, o proto e a
fonte do goneonize) e nao existe nele, entao responde 501 em vez de fingir:

- entrar num grupo da comunidade, cancelar o pedido e desativar a comunidade:
  nenhuma funcao (o whatsmeow so tem LinkGroup/UnlinkGroup entre pai e filho);
- mudar quem ve meus Status (`privacy/status`): so existe `get_status_privacy`.
  `SyncActionValue.statusPrivacy` existe no proto, mas nao se sabe indice nem
  colecao do aviso de estado, e um palpite errado diria "privado" a quem nao esta;
- remover contato: `send_app_state` recebe `PatchInfo` com `MutationInfo`
  (Index, Version, Value), sem campo de operacao, e o `EncodePatch` do whatsmeow
  grava sempre `SyncdMutation_SET`; remover e a operacao REMOVE;
- editar ou apagar canal: o whatsmeow tem a consulta `mutationUpdateNewsletter`
  mas nao a expoe, e `sendMexIQ` e privado; nao ha IQ cru no neonize;
- nivel de bateria: o multi-dispositivo nao entrega bateria do celular.

`unhealthy` nao entra de proposito: no servidor de referencia ele responde 503 e
encerra o processo (so serve para testar um orquestrador), e o cliente nao o usa.
`profile/:phone` tambem ficou de fora: e uma rota obsoleta (`getNumberProfile`,
@deprecated) que o cliente nao chama e cujo formato nao esta definido em lugar
nenhum que se possa conferir.

As funcoes puras ficam no topo (testadas em test_restantes.py); `registrar` liga
as rotas. Nada aqui foi testado contra uma conta real.
"""
import logging

from aiohttp import web

log = logging.getLogger("ponte")

# (metodo, rota, codigo, motivo). O `code` e o que o cliente le para decidir a
# mensagem: "unavailable" e o que o servidor de referencia usa quando nao ha
# resposta melhor (communityJoinController.ts).
IMPOSSIVEIS = (
    ("POST", "community-subgroups/{communityId}/{subgroupId}/join", "unavailable",
     "neonize has no way to join a community group"),
    ("POST", "community-subgroups/{communityId}/{subgroupId}/cancel-request", "unavailable",
     "neonize has no way to cancel a request to join a community group"),
    ("POST", "deactivate-community", "unavailable",
     "neonize has no way to deactivate a community"),
    ("POST", "privacy/status", "status_privacy_unsupported",
     "neonize can read, but not change, who sees my Status"),
    ("POST", "remove-contact", "contact_operation_unsupported",
     "neonize app state patches can only set an entry, not remove it"),
    ("PUT", "newsletter/{id}", "newsletter_unsupported",
     "neonize cannot edit a channel"),
    ("DELETE", "newsletter/{id}", "newsletter_unsupported",
     "neonize cannot delete a channel"),
    ("GET", "get-battery-level", "battery_unsupported",
     "a linked device does not receive the phone battery level"),
    ("POST", "meta-ai-terms/accept", "meta_ai_terms_unsupported",
     "accepting the Meta AI terms is not verified for this neonize build"),
)

# O aviso de termos da Meta AI que o WhatsApp Web usa (deviceController.ts,
# META_AI_NOTICE_ID_FALLBACK). Aceita-lo (AcceptTOSNotice) fica de fora: o estagio
# que a biblioteca pede nao esta confirmado para este aviso, e registrar um
# consentimento errado na conta do usuario nao se desfaz.
AVISO_META_AI = "20250502"


def plataforma_da_mensagem(serial) -> str:
    """'android', 'iphone', 'web' ou 'unknown' a partir do id da mensagem, pelas
    mesmas regras do wa-js (chat/functions/getPlatformFromMessage.ts, baseadas no
    Baileys): id com mais de 21 caracteres e android, '3A...' e iphone, '3EB0...'
    e web. `serial` e 'fromMe_conversa_id[_participante]' (ou so o id)."""
    texto = str(serial or "")
    partes = texto.split("_")
    id_curto = partes[2] if len(partes) >= 3 else texto
    if len(id_curto) > 21:
        return "android"
    if id_curto.startswith("3A"):
        return "iphone"
    if id_curto.startswith("3EB0"):
        return "web"
    return "unknown"


def termos_da_meta_ai(estado) -> dict:
    """O `response` de meta-ai-terms: `state` em maiusculas, como o cliente le."""
    return {"noticeId": AVISO_META_AI, "state": estado}


def registrar(r, *, sessao, ok):
    """Liga as rotas restantes ao roteador `r` do aiohttp."""

    def erro(codigo, status, mensagem=""):
        return web.json_response(
            {"status": "error", "code": codigo, "message": mensagem}, status=status)

    def impossivel(codigo, motivo):
        async def tratar(req):
            return erro(codigo, 501, motivo)
        return tratar

    async def meta_ai_terms(req):
        # O whatsmeow nao le o estado de um aviso de termos; "UNKNOWN" e o que o
        # cliente entende como "nao sei", sem bloquear o envio.
        return ok(status="success", response=termos_da_meta_ai("UNKNOWN"))

    async def platform_from_message(req):
        # Como o servidor de referencia: o corpo e a propria palavra, em JSON.
        return web.json_response(plataforma_da_mensagem(req.match_info["messageId"]))

    r.add_get("/api/{tok}/meta-ai-terms", meta_ai_terms)
    r.add_get("/api/{tok}/get-platform-from-message/{messageId}", platform_from_message)
    for metodo, rota, codigo, motivo in IMPOSSIVEIS:
        r.add_route(metodo, "/api/{tok}/" + rota, impossivel(codigo, motivo))
