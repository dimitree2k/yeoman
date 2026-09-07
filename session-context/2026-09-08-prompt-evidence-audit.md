# Prompt and evidence audit: Finanzgruppe / Ente

Read-only diagnosis, 2026-09-08 Europe/Berlin. No runtime, policy, persona or source behavior changes.

## Conclusion
The prompt stack contains strong verification instructions but also conflicting tool workflows and extensive persona/style content. The normal responder accepts text without enforcing successful evidence retrieval. This is a confirmed enforcement gap, not proof that prompt length alone causes hallucination.

## Current evidence
- Group names resolved against live chat_registry.db. Effective policies resolved with make_policy_engine(load_config()).resolve_policy.
- Finanzgruppe: alpha-2.md plus alpha-2.evolution.md. Ente: omega.md, no evolution file loaded. Both assistantDefault, configured OpenRouter ~z-ai/glm-flash-latest, temperature 0.9, medium reasoning.
- Reconstructed base system prompts: Finanzgruppe 54,796 characters / 7,715 whitespace words; Ente 52,421 / 7,383. These exclude history, retrieved memory, per-turn context and tool schemas. Counts are characters/words, not tokenizer counts or captured provider payloads.
- Bootstrap: workspace AGENTS.md, SOUL.md, USER.md; TOOLS.md and IDENTITY.md absent. Only selected persona is loaded, not every persona MD.
- Always-loaded skills: kanban, market-intelligence, summarize, ops. Full skill directory is summarized separately. Skill loading is not filtered by chat tool policy.
- Both groups allow web_search/web_fetch/deep_research/fact_check/browse. Only Finanzgruppe allows market_quote/market_intelligence. read_file and ops are absent from both allowlists despite skill instructions mentioning them.
- Gateway uses editable source under /home/dm/Documents/yeoman. During inspection PID changed externally from 2179809 to 2204460; no restart initiated here. New process environment has nonempty Tavily, Twelve Data, Finnhub and Alpaca credentials. Presence does not prove endpoint health; no external provider calls made.

## Findings and references
1. Missing evidence enforcement: adapters/responder_llm.py:1365 supplies tool definitions; :1665 accepts plain response.content. There is repair for deferred-work promises but no general freshness/evidence gate.
2. Conflicting market workflow: agent/context.py:227 requires structured quotes and forbids search snippets as quote data; always-loaded skills/market-intelligence/SKILL.md requires quote tools. Workspace AGENTS.md:18 instead mandates web_search/deep_research for current prices. Ente lacks both quote tools. Unavailable-tool instructions should lead to abstention, not guessing, but are internally inconsistent as executable workflow.
3. Weather: SOUL.md:84 broadly requests verification of time-sensitive facts when tools exist. No equally explicit weather-specific mandatory workflow or fail-closed enforcement.
4. Persona burden and certainty cues: alpha-2.md:54-60 rejects repetition/re-explanation; :65-69 requires grounded answers and correction. Evolution:18,27-28 asserts high domain confidence and consistently accurate macro analysis without per-turn evidence. These are plausible competing incentives, not measured causal effects. Omega:67 permits conditional unverified claims, weaker than mandatory verification of fresh facts.
5. Stale factual system instruction: agent/context.py:388 states no systemd units exist, contradicted by live yeoman-gateway user service. The prompt itself can supply false architectural facts.
6. Reply budgets are configured (600/560 hard caps), but reply_budget.py:155-165 exempts recognized sensitive/researched requests and explicitly preserves citations/caveats; therefore these limits do not prove facts are being stripped.

## Historical behavior and limitations
In Finanzgruppe session archive on 2026-07-07T07:44:57, a user challenged invented PC-release information. The following assistant correction asserted concrete GTA VI platform/release timing without an intervening tool_trace record. This supports the unchecked-correction failure mode; factual claims were not independently web-verified in this audit and current prompt equivalence is unknown. An AMD-current-move question on 2026-07-27 instead received an explicit inability to answer reliably. No specific weather-question exchange was found in the inspected archives. These observations do not establish a hallucination frequency or isolate model vs prompt causality.

## Recommended order
Unify one global evidence rule (including weather and current facts); align required tools with each chat allowlist and available credentials; enforce successful current evidence or explicit inability to verify in the answer path; shorten persona to style, remove invented competence and redundant constraints; evaluate with historical cases and controlled prompt/model comparisons. Changing temperature or adding another warning alone is not an evidence guarantee.
