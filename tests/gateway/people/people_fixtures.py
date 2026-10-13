"""Synthetic projected histories for person-directory tests (no real data)."""

from __future__ import annotations

from pathlib import Path

from tests.gateway.history_test_helpers import ProjectedHistory

T0 = 1_791_000_000_000
OWNER = "491757070305@s.whatsapp.net"
ALEX_LID, ALEX_PN = "203075365150770@lid", "4915000000001@s.whatsapp.net"
ALEXB_LID = "300000000002@lid"
FRANK_LID = "300000000003@lid"
LURKER_LID = "300000000004@lid"
BIGTALKER_LID = "300000000005@lid"
EXTRA_LID = "300000000006@lid"
DM_PN = "4915000000009@s.whatsapp.net"
ARVID = "4915202777685@s.whatsapp.net"


def _owner(history: ProjectedHistory) -> None:
    from yeoman_gateway.history.attestations import make
    history.attestations.append(make("contact", T0, "synthetic owner", identifiers=[OWNER],
                                      name="Synthetic Owner", role="owner"))
    history.attestations.append(make("contact", T0, "synthetic assistant", identifiers=[ARVID],
                                      name="Synthetic Arvid", role="assistant"))


def three_person_history(root: Path) -> ProjectedHistory:
    """One group with 'Alex Grrr', 'Alex B' and 'Frank'; the owner sends in it."""
    history = ProjectedHistory(root)
    _owner(history)
    history.add_group(chat_id="g@g.us", timestamp=T0, name="Small group", participants=[
        OWNER, {"phoneJid": ALEX_PN, "lid": ALEX_LID}, ALEXB_LID, FRANK_LID])
    history.add_message(chat_id="g@g.us", message_id="m1", text="a", timestamp=T0 + 1000,
                        participant=ALEX_LID, sender_name="Alex Grrr")
    history.add_message(chat_id="g@g.us", message_id="m2", text="b", timestamp=T0 + 2000,
                        participant=ALEXB_LID, sender_name="Alex B")
    history.add_message(chat_id="g@g.us", message_id="m3", text="c", timestamp=T0 + 3000,
                        participant=FRANK_LID, sender_name="Frank")
    history.add_message(chat_id="g@g.us", message_id="m4", text="d", timestamp=T0 + 4000,
                        participant=OWNER, sender_name="Owner")
    history.snapshot()
    return history


def level_history(root: Path) -> ProjectedHistory:
    """Small group G (owner, Alex, Alex B), big group B (5 members), a DM, an assistant message."""
    history = three_person_history(root)
    history.add_group(chat_id="big@g.us", timestamp=T0, name="Big group", participants=[
        OWNER, LURKER_LID, BIGTALKER_LID, EXTRA_LID, FRANK_LID])
    history.add_message(chat_id="big@g.us", message_id="b1", text="e", timestamp=T0 + 5000,
                        participant=BIGTALKER_LID, sender_name="Big Talker")
    history.add_message(chat_id=DM_PN, message_id="d1", text="f", timestamp=T0 + 6000,
                        sender_id=DM_PN, sender_name="Dee")
    history.add_message(chat_id="g@g.us", message_id="a1", text="g", timestamp=T0 + 7000,
                        sender_id="yeoman", direction="out")
    history.snapshot()
    return history
