"""Community routes: the pure functions that build what the client reads."""
from types import SimpleNamespace as N

import communities


def jid(user, server="s.whatsapp.net"):
    return N(User=user, Server=server)


def person(user, admin=False, super_admin=False, lid="", phone="", error=0):
    return N(JID=jid(user), LID=jid(lid, "lid"), PhoneNumber=jid(phone),
             IsAdmin=admin, IsSuperAdmin=super_admin, Error=error,
             AddRequest=N(Code="", Expiration=0))


def group(user, name="", parent="", parents=False, default=False, topic="", people=()):
    return N(JID=jid(user, "g.us"), GroupName=N(Name=name), GroupTopic=N(Topic=topic),
             GroupParent=N(IsParent=parents), GroupIsDefaultSub=N(IsDefaultSubGroup=default),
             GroupLinkedParent=N(LinkedParentJID=jid(parent, "g.us")),
             Participants=list(people))


def target(user, name, default=False):
    return N(JID=jid(user, "g.us"), GroupName=N(Name=name),
             GroupIsDefaultSub=N(IsDefaultSubGroup=default))


def test_only_the_parent_group_is_a_community():
    assert communities.is_community(group("1", parents=True))
    assert not communities.is_community(group("2", parent="1"))


def test_administrators_are_only_the_marked_ones_and_the_number_comes_first():
    g = group("1", parents=True, people=[
        person("lid1", admin=True, lid="lid1", phone="5511"), person("222", super_admin=True),
        person("333")])
    assert communities.administrators_of(g) == [
        {"id": "5511@s.whatsapp.net", "superAdmin": False},
        {"id": "222@s.whatsapp.net", "superAdmin": True}]


def test_am_administrator_by_number_or_by_lid():
    g = group("1", parents=True, people=[person("lid1", admin=True, lid="lid1"), person("333")])
    assert communities.am_administrator(g, {"lid1"})
    assert not communities.am_administrator(g, {"333"})   # member, not admin
    assert not communities.am_administrator(g, {""})
    assert not communities.am_administrator(g, set())


def test_joined_and_not_joined_subgroups():
    parent = group("1", "Community", parents=True)
    announcements = group("2", "Announcements", parent="1", default=True)
    general = group("3", "General", parent="1")
    from_another = group("4", "Other", parent="9")
    rows = communities.community_subgroups_of(
        parent, [parent, announcements, general, from_another],
        [target("2", "Announcements", True), target("5", "Not joined"), target("1", "the parent itself")])
    assert [(r["id"], r["subject"], r["joined"], r["announcement"]) for r in rows] == [
        ("2@g.us", "Announcements", True, True), ("3@g.us", "General", True, False),
        ("5@g.us", "Not joined", False, False)]
    assert rows[2]["approval"] is False and rows[2]["requested"] is False


def test_community_in_the_format_the_client_reads():
    parent = group("1", "Community", parents=True, topic="About",
                   people=[person("lid1", admin=True, lid="lid1")])
    announcements = group("2", "Announcements", parent="1", default=True)
    out = communities.communities_to_wpp([parent, announcements, group("7", "Ordinary group")], {"lid1"})
    assert out == [{
        "id": "1@g.us", "subject": "Community", "description": "About", "iAmAdmin": True,
        "admins": [{"id": "lid1@s.whatsapp.net", "superAdmin": False}],
        "subgroups": [{"id": "2@g.us", "subject": "Announcements", "announcement": True,
                       "general": False, "joined": True, "approval": False}]}]


def test_group_ids_refuses_what_is_not_a_group():
    assert communities.group_ids(["1@g.us", "12-34@g.us"]) == ["1@g.us", "12-34@g.us"]
    assert communities.group_ids([]) == []
    assert communities.group_ids(["5511@c.us"]) is None
    assert communities.group_ids("1@g.us") is None
    assert communities.group_ids([1]) is None


def test_link_result():
    assert communities.link_result("linkedGroupJids", ["1@g.us"], [("2@g.us", "x")]) == {
        "linkedGroupJids": ["1@g.us"],
        "failedGroups": [{"groupJid": "2@g.us", "errorText": "x"}]}
    assert communities.link_result("unlinkedGroupJids", [], []) == {
        "unlinkedGroupJids": [], "failedGroups": []}


def test_promotion_accepted_and_refused():
    assert communities.promotion_accepted("111@c.us", [person("111")])
    assert not communities.promotion_accepted("111@c.us", [])
    assert not communities.promotion_accepted("111@c.us", [person("111", error=403)])


def test_promotion_result_has_success_false_if_someone_was_refused():
    good = communities.promotion_result(["111@c.us"], [person("111")])
    assert good == {"participants": [{"id": "111@c.us", "code": 200}], "success": True}
    bad = communities.promotion_result(["111@c.us", "222@c.us"], [person("111")])
    assert bad["success"] is False and bad["participants"][1] == {"id": "222@c.us", "code": 403}


def test_community_people_mark_who_is_admin():
    admins = [person("lid1", admin=True, lid="lid1", phone="5511"), person("9", super_admin=True)]
    rows = communities.community_people(
        [jid("5511"), jid("9"), jid("333"), jid("")], admins)
    assert rows == [
        {"id": "5511@s.whatsapp.net", "isAdmin": True, "isSuperAdmin": False},
        {"id": "9@s.whatsapp.net", "isAdmin": True, "isSuperAdmin": True},
        {"id": "333@s.whatsapp.net", "isAdmin": False, "isSuperAdmin": False}]
