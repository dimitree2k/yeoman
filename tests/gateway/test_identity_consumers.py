from __future__ import annotations

import sqlite3
from pathlib import Path

from yeoman_gateway.history.reader import HistorySnapshot
from yeoman_gateway.history.schema import create
from yeoman_gateway.knowledge.api import open_knowledge_store
from yeoman_gateway.knowledge.authority import FakePolicyAuthority, FakeSourceAuthority

OWNER = "whatsapp:4910000000001"
IDENTIFIER = "4917000000000@s.whatsapp.net"


def test_value_resolution_fails_closed_for_conflicting_native_contact_owners(
    tmp_path: Path,
) -> None:
    """Two native owners of one full identifier are ambiguous to consumer lookups."""
    from yeoman_gateway.history.context import history_knowledge_scope

    from tests.gateway.convhist.test_hist_queries import contact, identifier
    from tests.gateway.history_test_helpers import prepare_history_retirement_knowledge

    db = sqlite3.connect(":memory:")
    create(db)
    contact(db, "native-account-one")
    contact(db, "native-account-two")
    identifier(db, "native-account-one", IDENTIFIER)
    identifier(db, "native-account-two", IDENTIFIER)
    snapshot = HistorySnapshot(1, (), db)
    knowledge = open_knowledge_store(
        prepare_history_retirement_knowledge(tmp_path, "ambiguous-native"),
        workspace_id="identity-consumer-tests",
        source_authority=FakeSourceAuthority(),
        policy_authority=FakePolicyAuthority(admins={OWNER}, capture_actors={OWNER}),
    )
    try:
        with history_knowledge_scope(snapshot, knowledge):
            assert knowledge.owners_of_identifier_value(IDENTIFIER) == ()
            assert knowledge.person_id_for_value(IDENTIFIER) is None
            assert knowledge.name_for_identifier(IDENTIFIER) is None
            assert knowledge.person_id_for_value("unknown-value") is None
    finally:
        knowledge.close()
        snapshot.close()
