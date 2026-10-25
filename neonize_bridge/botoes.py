"""Botoes de mensagens de empresa: os campos que o WinZapp le numa mensagem
(`client/core/business_buttons.py`) e a resposta a um botao. Funcoes puras, sem
servidor; `conversas_interativas.py` liga a rota `reply-button`.

Duas metades:

- `campos_de_botoes(mensagem)`: mensagem do whatsmeow (ja desembrulhada) -> os
  campos extras do formato WhatsApp Web (`dynamicReplyButtons`, `list`,
  `hydratedButtons`, `interactivePayload`). A POSICAO de cada botao na lista e o
  que o cliente devolve como `index`, entao um botao que o cliente nao sabe
  usar vira `{}` em vez de sair da lista.
- `plano_da_resposta(wpp, source, index)`: a mensagem guardada + o botao
  escolhido -> o que enviar, como dicionario simples; `montar_resposta` o
  transforma no proto.

Respostas que existem: botao de resposta ("dynamic", ButtonsResponseMessage),
linha de lista ("list", ListResponseMessage) e botao de modelo ("template",
TemplateButtonReplyMessage). O "native" (resposta rapida de mensagem
interativa) o cliente WhatsApp Web manda como texto com `selectedIndex` e
`selectedId`, e nenhum desses dois campos existe em ExtendedTextMessage: sem
formato confirmado, a ponte o recusa em vez de adivinhar.

Nada aqui foi testado contra uma conta real.
"""
SOURCES = ("dynamic", "native", "template", "list")


class BotaoRecusado(Exception):
    """Recusa com o codigo e o status HTTP que o servidor do WinZapp devolve."""

    def __init__(self, codigo, status):
        super().__init__(codigo)
        self.codigo = codigo
        self.status = status


def _tem(mensagem, campo):
    try:
        return mensagem.HasField(campo)
    except ValueError:
        return False


def _dinamicos(corpo):
    return [{"buttonId": b.buttonID,
             "buttonText": {"displayText": b.buttonText.displayText}, "type": 1}
            if b.buttonID and b.buttonText.displayText else {}
            for b in corpo.buttons if b.type != 2]      # 2 = NATIVE_FLOW


def _lista(corpo):
    return {"title": corpo.title, "description": corpo.description,
            "buttonText": corpo.buttonText,
            "sections": [{"title": sec.title, "rows": [
                {"rowId": row.rowID, "title": row.title, "description": row.description}
                for row in sec.rows]} for sec in corpo.sections]}


def _modelo(corpo):
    saida = []
    for i, b in enumerate(corpo.hydratedButtons):
        if _tem(b, "quickReplyButton"):
            saida.append({"index": i, "quickReplyButton": {
                "displayText": b.quickReplyButton.displayText, "id": b.quickReplyButton.ID}})
        elif _tem(b, "urlButton"):
            saida.append({"index": i, "urlButton": {
                "displayText": b.urlButton.displayText, "url": b.urlButton.URL}})
        elif _tem(b, "callButton"):
            saida.append({"index": i, "callButton": {
                "displayText": b.callButton.displayText,
                "phoneNumber": b.callButton.phoneNumber}})
        else:
            saida.append({})
    return saida


def campos_de_botoes(mensagem) -> dict:
    """Campos de botoes da mensagem do whatsmeow (ja desembrulhada); vazio se
    ela nao tem botoes."""
    if _tem(mensagem, "buttonsMessage"):
        b = mensagem.buttonsMessage
        return {"body": b.contentText or b.text,
                "dynamicReplyButtons": _dinamicos(b)}
    if _tem(mensagem, "listMessage"):
        return {"type": "list", "body": mensagem.listMessage.description,
                "list": _lista(mensagem.listMessage)}
    if _tem(mensagem, "templateMessage"):
        t = mensagem.templateMessage
        modelo = t.hydratedTemplate if _tem(t, "hydratedTemplate") else \
            t.hydratedFourRowTemplate
        return {"body": modelo.hydratedContentText,
                "hydratedButtons": _modelo(modelo)}
    if _tem(mensagem, "interactiveMessage"):
        i = mensagem.interactiveMessage
        if not _tem(i, "nativeFlowMessage"):
            return {}
        return {"body": i.body.text, "interactivePayload": {"buttons": [
            {"name": b.name, "buttonParamsJson": b.buttonParamsJSON}
            for b in i.nativeFlowMessage.buttons]}}
    return {}


def _item(lista, indice):
    if not isinstance(lista, list) or not 0 <= indice < len(lista) \
            or not isinstance(lista[indice], dict) or not lista[indice]:
        raise BotaoRecusado("button_not_found", 404)
    return lista[indice]


def plano_da_resposta(wpp, source, indice):
    """O que enviar ao tocar o botao `indice` de `source` na mensagem `wpp`
    (formato WhatsApp Web), ou BotaoRecusado."""
    if source == "dynamic":
        b = _item(wpp.get("dynamicReplyButtons"), indice)
        return {"tipo": "botao", "id": b["buttonId"],
                "texto": b["buttonText"]["displayText"]}
    if source == "template":
        b = _item(wpp.get("hydratedButtons"), indice).get("quickReplyButton")
        if not b:
            raise BotaoRecusado("button_not_found", 404)
        return {"tipo": "modelo", "id": b["id"], "texto": b["displayText"],
                "indice": indice}
    if source == "list":
        linhas = [row for sec in (wpp.get("list") or {}).get("sections") or []
                  for row in sec.get("rows") or []]
        row = _item(linhas, indice)
        if not row.get("rowId") or not row.get("title"):
            raise BotaoRecusado("button_not_found", 404)
        return {"tipo": "lista", "id": row["rowId"], "titulo": row["title"],
                "descricao": row.get("description") or ""}
    if source == "native":
        botao = _item((wpp.get("interactivePayload") or {}).get("buttons"), indice)
        if botao.get("name") != "quick_reply":
            raise BotaoRecusado("button_not_found", 404)
        raise BotaoRecusado("reply_button_unavailable", 501)
    raise BotaoRecusado("invalid_request", 400)


def montar_resposta(plano, ctx):
    """O plano -> `Message` do whatsmeow, citando a mensagem pelo `ctx`."""
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import (
        ButtonsResponseMessage, ListResponseMessage, Message, TemplateButtonReplyMessage)
    if plano["tipo"] == "botao":
        return Message(buttonsResponseMessage=ButtonsResponseMessage(
            selectedButtonID=plano["id"], selectedDisplayText=plano["texto"],
            type=ButtonsResponseMessage.DISPLAY_TEXT, contextInfo=ctx))
    if plano["tipo"] == "modelo":
        return Message(templateButtonReplyMessage=TemplateButtonReplyMessage(
            selectedID=plano["id"], selectedDisplayText=plano["texto"],
            selectedIndex=plano["indice"], contextInfo=ctx))
    return Message(listResponseMessage=ListResponseMessage(
        title=plano["titulo"], description=plano["descricao"],
        listType=ListResponseMessage.SINGLE_SELECT, contextInfo=ctx,
        singleSelectReply=ListResponseMessage.SingleSelectReply(selectedRowID=plano["id"])))
