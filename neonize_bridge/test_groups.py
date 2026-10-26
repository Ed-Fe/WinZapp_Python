"""Group routes: the pure functions that build what the client reads."""
from types import SimpleNamespace as N

import groups


def person(user, error=0, invite="", expires=0, lid="", phone=""):
    return N(JID=N(User=user), LID=N(User=lid), PhoneNumber=N(User=phone),
             Error=error, AddRequest=N(Code=invite, Expiration=expires))


def test_user_of_accepts_the_id_forms():
    assert groups.user_of("5511999@c.us") == "5511999"
    assert groups.user_of("5511999:12@s.whatsapp.net") == "5511999"
    assert groups.user_of("5511999") == "5511999"


def test_who_came_back_in_the_list_was_accepted_and_who_did_not_was_refused():
    out = groups.participants_result(
        ["111@c.us", "222@c.us"], [person("111")])
    assert out == {"111@c.us": {"code": 200}, "222@c.us": {"code": 403}}


def test_the_participants_error_becomes_the_code():
    out = groups.participants_result(
        ["111@c.us", "222@c.us"], [person("111", error=409), person("222", error=421)])
    assert out["111@c.us"]["code"] == 409 and out["222@c.us"]["code"] == 421


def test_refused_with_invitation_carries_the_invitation_code():
    out = groups.participants_result(
        ["111@c.us"], [person("111", error=403, invite="ABC", expires=99)])
    assert out["111@c.us"] == {"code": 403, "invite_code": "ABC", "invite_code_exp": 99}


def test_the_person_is_found_by_the_lid_or_by_the_number():
    assert groups.participants_result(
        ["333@lid"], [person("", lid="333")])["333@lid"]["code"] == 200
    assert groups.participants_result(
        ["444@c.us"], [person("", phone="444")])["444@c.us"]["code"] == 200


def test_join_errors_by_invitation():
    assert groups.entry_result(Exception("membership_approval_request")) == "pending"
    assert groups.entry_result(Exception("Membership approval required")) == "pending"
    assert groups.entry_result(Exception("already a participant")) == "member"
    assert groups.entry_result(
        Exception("info query returned status 409: conflict")) == "member"
    assert groups.entry_result(
        Exception("that group invite link has been revoked")) == "link"


def test_any_join_error_never_becomes_pending_request_nor_member():
    # a loose "request" and "participant" in the text gave "request sent" / "already a member"
    for text in ("info query returned status 400: bad-request", "expired",
                 "participant list unavailable", "request timed out", ""):
        assert groups.entry_result(Exception(text)) == "failure"


def test_requested_participant_with_the_ninth_digit_finds_the_one_that_came_back_without_it():
    without_ninth = person("551199990000")
    assert groups.participants_result(["5511999990000@c.us"], [without_ninth]) == {
        "5511999990000@c.us": {"code": 200}}
    # an LID gets no alternative form
    assert groups.participants_result(["5511999990000@lid"], [without_ninth]) == {
        "5511999990000@lid": {"code": 403}}


def test_requested_users():
    assert groups.requested_users("5511999990000@c.us") == ["5511999990000", "551199990000"]
    assert groups.requested_users("5511999990000@lid") == ["5511999990000"]
    assert groups.requested_users("1203-44@g.us") == ["1203-44"]


def test_common_groups_by_jid_lid_or_number():
    g1 = N(JID="g1@g.us", Participants=[person("111"), person("222")])
    g2 = N(JID="g2@g.us", Participants=[person("", lid="333")])
    g3 = N(JID="g3@g.us", Participants=[person("999")])
    assert groups.common_users([g1, g2, g3], {"111", "333"}) == ["g1@g.us", "g2@g.us"]
    assert groups.common_users([g1], set()) == []


def request(user, server="s.whatsapp.net", when=1700000000):
    return N(Participant=N(User=user, Server=server), TimeAt=when)


def test_join_request_becomes_the_row_the_client_reads():
    assert groups.request_to_wpp(request("111")) == {
        "id": "111@c.us", "method": "", "t": 1700000000}
    row = groups.request_to_wpp(request("333", "lid"), phone_of_lid="5511999", name="Ana")
    assert row == {"id": "333@lid", "method": "", "t": 1700000000,
                   "phone": "5511999@c.us", "name": "Ana"}


def test_not_administrator_error():
    assert groups.not_administrator(Exception("iq error: 403 forbidden"))
    assert not groups.not_administrator(Exception("timeout"))
