"""Which entries of the server's chat list are conversations WinZapp lists.

Statuses (@broadcast) go to the Status tab; channels (@newsletter) are not
conversations, and the Neonize bridge lists them without a name.
"""

_NOT_A_CONVERSATION = ("@broadcast", "@newsletter")


def is_listed_chat(jid) -> bool:
    return isinstance(jid, str) and bool(jid) and not jid.endswith(_NOT_A_CONVERSATION)
