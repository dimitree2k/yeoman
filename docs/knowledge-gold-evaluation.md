# Knowledge P0 evaluation

`yeoman eval knowledge-gold` uses explicit closed, checkpointed SQLite copies;
there are no runtime database defaults. Any WAL, SHM or journal sidecar is rejected.
Reads use SQLite `mode=ro&immutable=1` and `query_only`. Do not modify copies during
an evaluation. Schema 3 Knowledge and the current people/history schemas are required.
The Knowledge copy must contain a single workspace. Recall is lexical (no embedding
provider), through `KnowledgeService.recall(..., require_match=True)` as used by the
responder. Directory names in the question determine owner lookup targets; gold
person IDs never supply retrieval targets.

Create a private skeleton outside every Git worktree:

```sh
yeoman eval knowledge-gold-template --people-db /private/copies/people.db \
  --history-db /private/copies/history.db --persons 1,2,3 --out /private/gold.json
```

The coordinator fills questions and short expected facts and obtains owner confirmation.
The skeleton deliberately fails validation until completed. It copies no message text.
A completed item has this format (synthetic IDs and content):

```json
{
  "schema_version": 1,
  "items": [{
    "id": "K01",
    "question": "What does Synthetic Alex hold?",
    "reader": "owner_dm",
    "person_ids": [1],
    "case_type": "current_state",
    "expected_facts": [{
      "statement": "Holds synthetic stock A",
      "as_of": "2026-10-24",
      "source_message_ids": ["history-message-id"]
    }],
    "expected_outcome": "answer"
  }]
}
```

The validated versioned schema is `GoldFile` in
`packages/gateway/yeoman_gateway/evaluation/knowledge_gold.py` (JSON Schema is
available through `GoldFile.model_json_schema()`). Unknown fields, duplicate item
IDs, invalid dates/IDs/readers and answer cases without facts are rejected. Maximum
50 items; the report lists missing case types so a small development set cannot be
mistaken for full coverage. The baseline set should cover 15–20 people and all eight
case types: `current_state`, `change_over_time`, `self_vs_reported`, `same_first_name`,
`non_member_group_fact`, `unasked_owner_dm`, `decayed_fact`, `sparse_person`.

Readers are `owner_dm`, `group:<WhatsApp chat id>`, `person_dm:<directory id>` and
`non_member_dm`. Owner identity comes from the copied directory and is checked
against History. Group readers use the proven owner's membership and the whole
proven roster as recipients. Person DMs require a proven active endpoint.
Non-member DMs select the first proven directory outsider (excluding the owner
and involved people) outside the expected facts' source groups; without source
IDs, they must be outside all copied groups. Unknown membership or no suitable
reader makes the run fail, rather than manufacture a passing privacy case.

```sh
yeoman eval knowledge-gold --gold /private/gold.json \
  --knowledge-db /private/copies/knowledge.db --people-db /private/copies/people.db \
  --history-db /private/copies/history.db --out /private/baseline --dry-run
```

The output directory must be new. Gold paths and outputs are refused inside Git
worktrees, including symlinked paths and linked worktrees. Outputs are exclusively
created with mode 0600 (directories 0700); existing files are never overwritten.
Reports contain aggregate counts/metrics and item IDs with boolean results, never
questions, expected facts, answers, statements, people IDs or source IDs.

Gold recall is the fraction of expected facts on `answer` items for which **all**
listed History message IDs occur among recalled statements' sources. Legacy source
aliases are translated to History message IDs. This is a source-coverage proxy;
it does not establish semantic precision, attribution or temporal correctness.
`refuse`, `dont_know` and `nothing_injected` pass deterministic scoring only when
recall is entirely empty. Privacy leaks count nonempty `refuse`/`nothing_injected`
items; refusal correctness is the fraction of `refuse` items passing the checks.

Without `--grader-route` (or with `--dry-run`), no configuration/provider is loaded
and Answer-from-Knowledge is unmeasured. With a route, the existing route client
uses fixed prompt version `knowledge-gold-v1`: one answer call sees only question
and Knowledge, followed by one grading call with the expected facts/as-of dates.
There are no history tools. Answer-from-Knowledge uses only `answer` items as its
denominator. Graded refusal correctness requires both empty recall and a passing
grade; per-item overall pass also requires both layers. Invalid grader output aborts
with a content-free CLI error. Model/route/prompt versions are recorded for replay.
The optional route transmits the in-memory question, recalled context and expected
facts to its configured provider; none is persisted by this harness.

`report.json` and `report.md` include the §7.4 baseline rows. Unmeasured precision,
temporal correctness and extractor parse rate remain null/dashes. No baseline from
real owner data is implied by synthetic tests or by generating a skeleton.
