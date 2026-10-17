"""The person filter must not run as a per-statement correlated subquery.

On the live store a correlated ``EXISTS`` over ``knowledge_statement_people`` made an
owner-DM recall take ~6 s and froze the gateway long enough for the health check to
restart it (2026-10-16).
"""

from __future__ import annotations


def test_person_filter_is_not_a_correlated_subquery(knowledge_harness):
    h = knowledge_harness
    alex = h.person("Alex")
    engine = h.service._retrieval  # noqa: SLF001 - white-box query-plan assertion
    clause, params = engine._person_filter_clause((alex,), ())  # noqa: SLF001
    assert clause
    rows = h.service._store.query(  # noqa: SLF001
        f"EXPLAIN QUERY PLAN SELECT s.statement_id FROM knowledge_statements s WHERE {clause}",
        tuple(params),
    )
    plan = " | ".join(str(tuple(row)[-1]) for row in rows)
    # The bad plan scanned every statement and probed the links once per row
    # ("SCAN s ... SEARCH kp EXISTS"); the good one looks statements up by id.
    assert "SCAN s " not in plan, plan
    assert "EXISTS" not in plan, plan
