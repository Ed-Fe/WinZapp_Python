from core.listed_chats import is_listed_chat


def test_people_and_groups_are_listed():
    for jid in ("5511999990000@s.whatsapp.net", "77@lid", "1203-44@g.us"):
        assert is_listed_chat(jid)


def test_channels_and_statuses_are_not_listed():
    for jid in ("120363@newsletter", "status@broadcast", "123@broadcast", "", None):
        assert not is_listed_chat(jid)
