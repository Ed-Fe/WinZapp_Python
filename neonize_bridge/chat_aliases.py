"""One chat, two addresses: the number (@s.whatsapp.net) and the LID (@lid).

The store keeps the chat under the exact key the event brought, but the client
prefers the @lid when it knows it and in the routes may send either of the
two. Without joining the two, get-messages answers 200 [] for the other
address (and the client only tries another address when the status is not
200), send-seen does not find the row, and archive, pin and mute create a new
empty row without touching the real one: the next list-chats comes back as it
was and the client's two-way sync undoes the action.

`Session` inherits `ChatAlias` (server.py). The choice logic is the pure
function `chat_keys`, tested in test_chat_aliases.py; the number <-> LID
mapping comes from neonize's store (nothing was tested against a real account).
"""
import msgmap

SERVERS_WITH_ALIAS = ("@s.whatsapp.net", "@lid")


def forms(jid):
    """The JID in whatsmeow's form, plus the legacy @c.us form of the number."""
    base = msgmap.jid_whatsmeow(jid)
    if base.endswith("@s.whatsapp.net"):
        return [base, base[:-len("@s.whatsapp.net")] + "@c.us"]
    return [base]


def chat_keys(exists, jid, other_address):
    """The keys, in the store, of `jid`'s chat: the given one first, then the
    other address (number <-> LID). `exists(key)` says whether the row exists;
    `other_address(jid)` returns the other address or '' if it does not know it."""
    candidates = forms(jid)
    base = candidates[0]
    if base.endswith(SERVERS_WITH_ALIAS):
        other = other_address(base)
        if other:
            candidates += forms(other)
    return [c for c in dict.fromkeys(candidates) if exists(c)]


def _text(jid):
    return f"{jid.User}@{jid.Server}" if getattr(jid, "User", "") else ""


class ChatAlias:
    """`Session` methods that resolve the addresses of a chat."""

    def _other_address(self, jid):
        """The LID of a number or the number of an LID, through neonize; '' if unknown."""
        if self.client is None or self.status != "CONNECTED":
            return ""
        from neonize.utils import build_jid
        user, _, server = jid.partition("@")
        try:
            target = build_jid(user, server)
            found = _text(self.client.get_pn_from_lid(target) if server == "lid"
                          else self.client.get_lid_from_pn(target))
            return msgmap.jid_whatsmeow(found) if found else ""
        except Exception:
            return ""

    def chat_keys(self, jid):
        """All the stored keys of the chat (the given one first); [] if it
        does not yet exist in the store."""
        return chat_keys(self.db.chat_exists, jid, self._other_address)

    def existing_chat(self, jid):
        """The key under which the chat is stored (that of the first address that
        exists), or '' if none exists. Blocks on a neonize call: call it from a
        thread."""
        keys = self.chat_keys(jid)
        return keys[0] if keys else ""
