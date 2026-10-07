"""Identity resolution: one contact per person (spec: Identity resolution, steps 1-7)."""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import Any

from .attestations import Attestation
from .ids import Ident, classify, numeric_part

NAMESPACE = uuid.UUID("5b0f9d4e-2c61-5f0a-8d3e-6a7c1e2f9b40")
REF = "ref:"
MAX_REFS = 50
_EDGE_PRIORITY = {"owner_attested": 0, "native_pair": 1, "knowledge_binding": 2}
_EVIDENCE_ORDER = ("owner_attested", "native_pair", "knowledge_binding", "observed", "numeric_match")
_NODE_KINDS = frozenset({"lid", "pn_jid", "newsletter", "numeric"})
_INF = float("inf")


@dataclass
class Sighting:
    first_ms: int | None = None
    last_ms: int | None = None
    first_ref: str | None = None

    def add(self, ms: int | None, ref: str | None) -> None:
        if ms is not None:
            if self.first_ms is None or ms < self.first_ms:
                self.first_ms, self.first_ref = ms, ref
            if self.last_ms is None or ms > self.last_ms:
                self.last_ms = ms
        if self.first_ref is None:
            self.first_ref = ref

    def merge(self, other: Sighting) -> None:
        self.add(other.first_ms, other.first_ref)
        self.add(other.last_ms, other.first_ref)


@dataclass(frozen=True)
class ContactRecord:
    contact_ref: str
    created_ms: int | None
    display_name: str | None
    preferred_name: str | None
    ref: str


@dataclass(frozen=True)
class IdentityLink:
    a: str
    b: str
    evidence: str
    ref: str
    occurred_ms: int | None = None
    time_basis: str = "unknown"
    last_ms: int | None = None
    valid_from_ms: int | None = None
    valid_until_ms: int | None = None


@dataclass
class IdentityInput:
    sightings: dict[Ident, Sighting] = field(default_factory=dict)
    groups: set[Ident] = field(default_factory=set)
    links: list[tuple[str, str, str, str]] = field(default_factory=list)
    link_times: dict[tuple[str, str, str, str], IdentityLink] = field(default_factory=dict)

    names: dict[str, list[tuple[int | None, str, str]]] = field(default_factory=lambda: defaultdict(list))
    contact_records: dict[str, ContactRecord] = field(default_factory=dict)
    attestations: list[Attestation] = field(default_factory=list)

    @property
    def timed_links(self) -> list[IdentityLink]:
        # Keep the existing four-field link surface for extraction consumers.
        return [self.link_times.get(link) or IdentityLink(*link) for link in self.links]

    def see(self, ident: Ident | None, ms: int | None, ref: str) -> None:
        if ident is None:
            return
        if ident.kind == "group":
            self.groups.add(ident)
        elif ident.kind in _NODE_KINDS:
            self.sightings.setdefault(ident, Sighting()).add(ms, ref)

    def link(self, a: Ident | None, b: Ident | None, evidence: str, ref: str, *,
             occurred_ms: int | None = None, time_basis: str = "unknown",
             last_ms: int | None = None) -> None:
        if a is None or b is None or not (a.strong and b.strong) or a == b:
            return
        self.see(a, None, ref)
        self.see(b, None, ref)
        key = (a.value, b.value, evidence, ref)
        self.links.append(key)
        self.link_times[key] = IdentityLink(*key, occurred_ms, time_basis, last_ms)

    def bind(self, contact_ref: str, ident: Ident | None, ref: str, *,
             valid_from_ms: int | None = None, valid_until_ms: int | None = None) -> None:
        if ident is None or not ident.strong:
            return
        self.see(ident, None, ref)
        key = (REF + contact_ref, ident.value, "knowledge_binding", ref)
        self.links.append(key)
        self.link_times[key] = IdentityLink(*key, valid_from_ms=valid_from_ms,
                                           valid_until_ms=valid_until_ms)

    def name(self, node: str, name: Any, ms: int | None, ref: str) -> None:
        if name is not None and str(name).strip():
            self.names[node].append((ms, str(name).strip(), ref))

    def contact_record(self, record: ContactRecord) -> None:
        self.contact_records.setdefault(record.contact_ref, record)


@dataclass(frozen=True)
class ContactRow:
    contact_id: str
    kind: str
    role: str | None
    display_name: str | None
    status: str
    merged_into: str | None
    source_refs: tuple[str, ...]


@dataclass(frozen=True)
class IdentRow:
    contact_id: str
    channel: str
    kind: str
    value: str
    strength: str
    evidence: str
    first_seen_ms: int | None
    last_seen_ms: int | None
    ended_ms: int | None
    source_refs: tuple[str, ...]
    valid_from_ms: int | None = None
    valid_until_ms: int | None = None


def _compatible(start: int | None, end: int | None, ms: int | None, basis: str,
                last_ms: int | None = None) -> bool:
    if ms is None or basis == "unknown":
        return True
    if basis == "capture_time_approx":
        # Capture is an upper bound on occurrence; there is no invented lower bound.
        return start is None or start <= ms
    return (end is None or ms < end) and (start is None or start <= (last_ms if last_ms is not None else ms))


@dataclass
class Resolution:
    contacts: list[ContactRow]
    identifiers: list[IdentRow]
    node_contact: dict[str, str]
    role_contact: dict[str, str]
    canonical: dict[str, Ident]
    review: dict[str, list[dict[str, Any]]]

    _identifier_index: dict[tuple[str, str], list[IdentRow]] = field(init=False, repr=False, compare=False)
    _contact_snapshot: list[ContactRow] | None = field(default=None, init=False, repr=False, compare=False)
    _redirects: dict[str, str] = field(default_factory=dict, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._identifier_index = defaultdict(list)
        for row in self.identifiers:
            self._identifier_index[(row.kind, row.value)].append(row)

    def terminal(self, contact_id: str) -> str:
        if self._contact_snapshot is not self.contacts:
            self._redirects = {c.contact_id: c.merged_into for c in self.contacts if c.merged_into}
            self._contact_snapshot = self.contacts
        redirects = self._redirects
        seen: set[str] = set()
        while contact_id in redirects:
            if contact_id in seen:
                raise ValueError("contact redirect cycle")
            seen.add(contact_id)
            contact_id = redirects[contact_id]
        return contact_id

    def resolve(self, ident: Ident | None, *, occurred_ms: int | None = None,
                time_basis: str = "unknown") -> tuple[str | None, str]:
        if ident is None:
            return None, "unknown"
        if ident.kind == "group":
            return None, "group"
        if ident.kind == "assistant":
            contact = self.role_contact.get("assistant")
            return (self.terminal(contact), "exact") if contact else (None, "unknown")
        match = "exact"
        if ident.kind == "numeric" and ident.value in self.canonical:
            ident = self.canonical[ident.value]
            if ident.kind == "group":
                return None, "group"
            match = "numeric_match"
        rows = [i for i in self._identifier_index.get((ident.kind, ident.value), [])
                if _compatible(i.valid_from_ms, i.valid_until_ms, occurred_ms, time_basis)]
        owners = {self.terminal(i.contact_id) for i in rows}
        if len(owners) == 1:
            return next(iter(owners)), match
        return None, "unknown"

    def contact_for_anchor(self, value: str) -> str | None:
        return self.resolve(classify(value))[0]


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def add(self, node: str) -> None:
        self.parent.setdefault(node, node)

    def find(self, node: str) -> str:
        self.add(node)
        root = node
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[node] != root:
            self.parent[node], node = root, self.parent[node]
        return root

    def union(self, a: str, b: str, forbidden: list[tuple[str, str]]) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return True
        for x, y in forbidden:
            if {self.find(x), self.find(y)} == {ra, rb}:
                return False
        low, high = sorted((ra, rb))
        self.parent[high] = low
        return True


def _identifier_values(att: Attestation) -> list[str]:
    values = [att.fields[k] for k in ("anchor", "identifier", "a", "b") if k in att.fields]
    return [*values, *att.fields.get("identifiers", [])]


def _cap(refs: set[str | None]) -> tuple[str, ...]:
    return tuple(sorted(r for r in refs if r)[:MAX_REFS])


def _value(raw: Any) -> str:
    ident = classify(raw)
    assert ident is not None and ident.strong, raw
    return ident.value


@dataclass
class _Window:
    value: str
    start: int | None
    end: int | None
    anchor: str | None = None
    refs: set[str] = field(default_factory=set)
    ended_ms: int | None = None

    @property
    def node(self) -> str:
        if self.anchor is None and self.start is None and self.end is None:
            return self.value
        return f"window:{self.value}:{self.start}:{self.end}:{self.anchor or ''}"


def resolve(inp: IdentityInput) -> Resolution:
    links = inp.timed_links
    review: dict[str, list[dict[str, Any]]] = {
        "numeric_ambiguous": [], "merged_knowledge_contacts": [], "blocked_by_unmerge": [],
        "identifier_ended_not_applied": [], "temporal_links_ambiguous": [],
        "identifier_conflicts": []}
    attested = sorted(inp.attestations, key=lambda a: (a.at_ms, a.ref))
    strong_known = {i for i in inp.sightings if i.strong}
    for link in links:
        strong_known |= {i for i in (classify(link.a), classify(link.b)) if i is not None and i.strong}
    for att in attested:
        strong_known |= {i for v in _identifier_values(att) if (i := classify(v)) is not None and i.strong}
    index: dict[str, set[Ident]] = defaultdict(set)
    for ident in strong_known | inp.groups:
        index[numeric_part(ident.value)].add(ident)
    sightings = {i: Sighting() for i in strong_known}
    for ident, seen in inp.sightings.items():
        if ident.strong:
            sightings[ident].merge(seen)
    names = {node: list(items) for node, items in inp.names.items()}
    canonical: dict[str, Ident] = {}
    numeric_evidence: set[str] = set()
    for ident in sorted(i for i in inp.sightings if i.kind == "numeric"):
        candidates = index.get(ident.value, set())
        if len(candidates) == 1:
            target = next(iter(candidates))
            canonical[ident.value] = target
            if target.strong:
                sightings[target].merge(inp.sightings[ident])
                numeric_evidence.add(target.value)
                names.setdefault(target.value, []).extend(names.pop(ident.value, []))
            continue
        if len(candidates) > 1:
            review["numeric_ambiguous"].append(
                {"value": ident.value, "candidates": sorted(c.value for c in candidates)})
        sightings[ident] = inp.sightings[ident]
    idents = {i.value: i for i in sightings}

    windows: dict[str, list[_Window]] = defaultdict(list)
    for att in attested:
        if att.type == "identifier":
            ident = classify(att.fields["identifier"])
            if ident is not None and ident.strong:
                windows[ident.value].append(_Window(ident.value, att.fields.get("valid_from_ms"),
                    att.fields.get("valid_until_ms"), _value(att.fields["anchor"]), {att.ref}))
    for link in links:
        if link.evidence == "knowledge_binding" and (link.valid_from_ms is not None or link.valid_until_ms is not None):
            windows[link.b].append(_Window(link.b, link.valid_from_ms, link.valid_until_ms, link.a, {link.ref}))
    explicit = set(windows)
    prior_contacts: dict[str, _Window] = {}
    for att in attested:
        if att.type == "contact":
            values = [i.value for v in att.fields["identifiers"] if (i := classify(v)) is not None and i.strong]
            for value in values:
                prior = _Window(value, None, None, values[0] if value != values[0] else None, {att.ref})
                prior_contacts.setdefault(prior.node, prior).refs.add(att.ref)
                if value not in explicit:
                    windows[value].append(replace(prior, refs=set(prior.refs)))
    contact_asserted = set(windows)
    for link in links:
        if link.evidence == "knowledge_binding" and link.b not in contact_asserted:
            windows[link.b].append(_Window(link.b, None, None, link.a, {link.ref}))
    # Repeated copies of one assertion are one applicable binding, with all refs.
    unique: dict[str, _Window] = {}
    for items in windows.values():
        for w in items:
            if w.node in unique:
                unique[w.node].refs.update(w.refs)
            else:
                unique[w.node] = w
    windows = defaultdict(list)
    for w in unique.values():
        windows[w.value].append(w)
    # Alias lineage retains supported earlier seeds, including contact assertions
    # suppressed by explicit/timed ownership. It never adds ownership edges.
    current_windows = [w for items in windows.values() for w in items]
    alias_windows = {w.node: replace(w, refs=set(w.refs)) for w in current_windows}
    for source_windows in (list(prior_contacts.values()), current_windows):
        for att in attested:
            if att.type != "identifier_ended":
                continue
            ident = classify(att.fields["identifier"])
            if ident is None or not ident.strong:
                continue
            end = att.fields["ended_ms"]
            applicable = [w for w in source_windows if w.value == ident.value
                          and (w.start is None or w.start < end) and (w.end is None or end <= w.end)]
            if len(applicable) == 1:
                w = applicable[0]
                alias_windows.setdefault(w.node, replace(w, refs=set(w.refs)))
                w.end = end
                w.ended_ms = end
                w.refs.add(att.ref)
                alias_windows[w.node] = replace(w, refs=set(w.refs))
            elif source_windows is current_windows:
                review["identifier_ended_not_applied"].append({
                    "ref": att.ref, "identifier": ident.value, "ended_ms": end,
                    "resolution": "absent" if not applicable else "multiple"})

    # Only native-pair components inherit cuts. Person anchors and Knowledge UUIDs
    # stay stable; an old LID must never bridge the two sides of a phone handover.
    pair_graph = _UnionFind()
    for link in links:
        if link.evidence == "native_pair":
            pair_graph.union(link.a, link.b, [])
    cuts: dict[str, set[int]] = defaultdict(set)
    for value, items in windows.items():
        cuts[pair_graph.find(value)].update(t for w in items for t in (w.start, w.end) if t is not None)
    anchors = {w.anchor for items in windows.values() for w in items if w.anchor}
    for value in idents:
        if windows.get(value):
            continue
        bounds = [] if value in anchors else sorted(cuts.get(pair_graph.find(value), set()))
        edges: list[int | None] = [None, *bounds, None]
        windows[value] = [_Window(value, start, end) for start, end in zip(edges, edges[1:])]
    # Identical assertions share a node but keep every source reference.
    node_window: dict[str, _Window] = {}
    for items in windows.values():
        for w in items:
            if w.node in node_window:
                node_window[w.node].refs.update(w.refs)
            else:
                node_window[w.node] = w
    windows = defaultdict(list)
    for w in node_window.values():
        windows[w.value].append(w)
    uf = _UnionFind()
    for node in node_window:
        uf.add(node)
    for contact_ref in inp.contact_records:
        uf.add(REF + contact_ref)
    evidence_of: dict[str, set[str]] = defaultdict(set)
    refs_of: dict[str, set[str | None]] = defaultdict(set)
    for node, w in node_window.items():
        refs_of[node].update(w.refs)
        if w.refs:
            evidence_of[node].add("owner_attested" if not w.anchor or not w.anchor.startswith(REF) else "knowledge_binding")
        if idents[w.value] in inp.sightings:
            evidence_of[node].add("observed")
        if w.value in numeric_evidence:
            evidence_of[node].add("numeric_match")

    def nodes(value: str, ms: int | None = None, basis: str = "unknown", last: int | None = None) -> list[str]:
        if value.startswith(REF):
            return [value]
        return [w.node for w in windows[value] if _compatible(w.start, w.end, ms, basis, last)]

    pair_intent: dict[tuple[str, str], Attestation] = {}
    for att in attested:
        if att.type in ("merge", "unmerge"):
            pair_intent[tuple(sorted((_value(att.fields["a"]), _value(att.fields["b"]))))] = att
    forbidden = [(x, y) for pair, att in pair_intent.items() if att.type == "unmerge"
                 for x in nodes(pair[0]) for y in nodes(pair[1])]

    temporal_forbidden: list[tuple[str, str]] = []

    def join(a: str, b: str, evidence: str, ref: str) -> None:
        if uf.union(a, b, forbidden + temporal_forbidden):
            for node in (a, b):
                evidence_of[node].add(evidence)
                refs_of[node].add(ref)
        else:
            manual = any({uf.find(x), uf.find(y)} == {uf.find(a), uf.find(b)} for x, y in forbidden)
            review["blocked_by_unmerge" if manual else "temporal_links_ambiguous"].append({
                "a": node_window[a].value if a in node_window else a,
                "b": node_window[b].value if b in node_window else b, "evidence": evidence, "ref": ref})

    # Resolve dependencies to a fixed point: multiple anchor windows are usable
    # only when all compatible windows already have the same owner root.
    pending = [(node, w) for node, w in node_window.items() if w.anchor]
    while pending:
        deferred = []
        for node, w in pending:
            anchors = (nodes(w.anchor) if w.anchor.startswith(REF) else [
                anchor.node for anchor in windows[w.anchor]
                if max(w.start if w.start is not None else -_INF,
                       anchor.start if anchor.start is not None else -_INF)
                < min(w.end if w.end is not None else _INF,
                      anchor.end if anchor.end is not None else _INF)])
            if len({uf.find(anchor) for anchor in anchors}) == 1:
                for ref in sorted(w.refs):
                    for anchor in anchors:
                        join(node, anchor, "knowledge_binding" if w.anchor.startswith(REF) else "owner_attested", ref)
            else:
                deferred.append((node, w))
        if len(deferred) == len(pending):
            break
        pending = deferred
    for _, w in pending:
        review["temporal_links_ambiguous"].append({"a": w.anchor, "b": w.value,
            "ref": min(w.refs), "evidence": "owner_attested"})
    for att in attested:
        values = [i.value for v in _identifier_values(att) if (i := classify(v)) is not None and i.strong]
        for value in values:
            for node in nodes(value):
                evidence_of[node].add("owner_attested")
                refs_of[node].add(att.ref)
        if att.type == "contact":
            for value in values[1:]:
                # Contact assertions do not erase transferred-identifier windows.
                if len(nodes(values[0])) == len(nodes(value)) == 1:
                    join(nodes(values[0])[0], nodes(value)[0], "owner_attested", att.ref)
        elif att.type == "merge" and pair_intent[tuple(sorted(values))].ref == att.ref:
            for a in nodes(values[0]):
                for b in nodes(values[1]):
                    join(a, b, "owner_attested", att.ref)

    # Different owners of the same value must remain separate even when an old
    # unbounded LID or Knowledge binding supplies a transitive route between them.
    # Explicit accepted merges above may already have made these the same person.
    for items in windows.values():
        for n, a in enumerate(items):
            for b in items[n + 1:]:
                if a.anchor and b.anchor and a.anchor != b.anchor and uf.find(a.node) != uf.find(b.node):
                    temporal_forbidden.append((a.node, b.node))

    links = sorted(links, key=lambda link: (_EDGE_PRIORITY[link.evidence],
        link.occurred_ms is None or link.time_basis == "unknown", link.occurred_ms or 0, link.ref))
    for link in links:
        if link.evidence == "knowledge_binding" and any(
                w.anchor == link.a and link.ref in w.refs for w in windows.get(link.b, [])):
            continue  # Already represented by its ownership edge.
        aa, bb = nodes(link.a, link.occurred_ms, link.time_basis, link.last_ms), nodes(link.b, link.occurred_ms, link.time_basis, link.last_ms)
        if not aa or not bb or len({uf.find(n) for n in aa}) != 1 or len({uf.find(n) for n in bb}) != 1:
            review["temporal_links_ambiguous"].append({"a": link.a, "b": link.b,
                                                      "evidence": link.evidence, "ref": link.ref})
            continue
        for a in aa:
            for b in bb:
                join(a, b, link.evidence, link.ref)

    role_by_root: dict[str, str] = {}
    name_by_root: dict[str, str] = {}
    for att in attested:
        if att.type == "contact":
            projected = [i.value for v in att.fields["identifiers"] if (i := classify(v)) is not None and i.strong]
            if not projected:
                continue
            roots = {uf.find(n) for n in nodes(projected[0])}
            if len(roots) != 1:
                continue
            root = next(iter(roots))
            if att.fields.get("role"):
                role_by_root[root] = att.fields["role"]
            if att.fields.get("name"):
                name_by_root[root] = att.fields["name"]
        elif att.type == "name":
            roots = {uf.find(n) for n in nodes(_value(att.fields["anchor"]))}
            if len(roots) == 1:
                name_by_root[next(iter(roots))] = att.fields["name"]
    components: dict[str, list[str]] = defaultdict(list)
    for node in list(uf.parent):
        components[uf.find(node)].append(node)
    contacts: list[ContactRow] = []
    identifiers: list[IdentRow] = []
    node_contact: dict[str, str] = {}
    role_contact: dict[str, str] = {}
    for root in sorted(components, key=lambda r: min(components[r])):
        members = sorted(components[root])
        refs = [m[len(REF):] for m in members if m.startswith(REF)]
        owned = [node_window[m] for m in members if m in node_window]
        strong = [w for w in owned if idents[w.value].strong]
        records = [inp.contact_records[r] for r in refs if r in inp.contact_records]
        if refs:
            chosen = min(refs, key=lambda r: (inp.contact_records[r].created_ms
                if r in inp.contact_records and inp.contact_records[r].created_ms is not None else _INF, r))
        elif strong:
            first = min(strong, key=lambda w: (w.start is not None or w.end is not None,
                sightings[idents[w.value]].first_ms if sightings[idents[w.value]].first_ms is not None else _INF, w.value, w.node))
            chosen = str(uuid.uuid5(NAMESPACE, first.node if first.start is not None or first.end is not None else first.value))
        else:
            chosen = str(uuid.uuid5(NAMESPACE, "numeric:" + owned[0].value))
        for member in members:
            node_contact[member] = chosen
        role = role_by_root.get(root)
        if role:
            role_contact[role] = chosen
        push: dict[str, Sighting] = {}
        for member in members:
            w = node_window.get(member)
            for ms, name, ref in names.get(w.value if w else member, []):
                if w is None or _compatible(w.start, w.end, ms, "provider_timestamp"):
                    push.setdefault(name, Sighting()).add(ms, ref)
        latest_push = max(push, key=lambda n: (push[n].last_ms or -1, n)) if push else None
        record = inp.contact_records.get(chosen)
        display = (name_by_root.get(root) or (record.preferred_name if record else None)
                   or (record.display_name if record else None) or latest_push)
        status = "confirmed" if strong or refs else "provisional"
        kind = "channel" if any(idents[w.value].kind == "newsletter" for w in owned) else "person"
        contact_refs: set[str | None] = {r.ref for r in records}
        for member in members:
            contact_refs |= refs_of[member]
        for w in owned:
            contact_refs.add(sightings[idents[w.value]].first_ref)
        contacts.append(ContactRow(chosen, kind, role, display, status, None, _cap(contact_refs)))
        for other in sorted(r for r in refs if r != chosen):
            rec = inp.contact_records.get(other)
            contacts.append(ContactRow(other, kind, None, rec.display_name if rec else None, "confirmed",
                                       chosen, _cap({rec.ref} if rec else set())))
        if len(refs) > 1:
            review["merged_knowledge_contacts"].append({"contact_id": chosen,
                "merged": sorted(r for r in refs if r != chosen),
                "names": sorted({r.display_name for r in records if r.display_name})})
        for w in owned:
            ident, seen = idents[w.value], sightings[idents[w.value]]
            present = evidence_of[w.node]
            evidence = next((e for e in _EVIDENCE_ORDER if e in present), "observed")
            identifiers.append(IdentRow(chosen, "whatsapp", ident.kind, ident.value,
                "strong" if ident.strong else "weak", evidence, seen.first_ms, seen.last_ms, w.ended_ms,
                _cap(refs_of[w.node] | {seen.first_ref}), w.start, w.end))
        for name in sorted(push):
            seen = push[name]
            identifiers.append(IdentRow(chosen, "whatsapp", "push_name", name, "weak", "observed",
                                        seen.first_ms, seen.last_ms, None, _cap({seen.first_ref})))
    res = Resolution(contacts, identifiers, node_contact, role_contact, canonical, review)
    # Previously exposed per-identifier UUIDs remain addressable after merging.
    rows = {c.contact_id: c for c in contacts}
    for value, ident in sorted(idents.items()):
        contact = res.resolve(ident)[0]
        if contact:
            node_contact[value] = contact
            generated = str(uuid.uuid5(NAMESPACE, ("numeric:" if ident.kind == "numeric" else "") + value))
            if generated not in rows:
                c = rows[contact]
                rows[generated] = ContactRow(generated, c.kind, None, None, c.status, contact, c.source_refs)
    # Bounded nodes also expose UUIDs before a later Knowledge binding/merge.
    # Their seed is stable even when the surviving contact changes.
    for node, w in sorted(node_window.items()):
        if idents[w.value].strong and (w.start is not None or w.end is not None):
            contact = res.terminal(node_contact[node])
            generated = str(uuid.uuid5(NAMESPACE, node))
            if generated not in rows:
                c = rows[contact]
                rows[generated] = ContactRow(generated, c.kind, None, None, c.status, contact, c.source_refs)
    # Former native-pair slices derive from the same retained window evidence.
    # A finer current slice must not erase a uniquely owned earlier slice alias.
    pair_members: dict[str, list[str]] = defaultdict(list)
    for value in idents:
        pair_members[pair_graph.find(value)].append(value)
    for w in list(alias_windows.values()):
        if w.start is None and w.end is None:
            continue
        for value in pair_members[pair_graph.find(w.value)]:
            if value != w.value:
                prior = _Window(value, w.start, w.end, refs=set(w.refs))
                alias_windows.setdefault(prior.node, prior)
    for seed, prior in sorted(alias_windows.items()):
        if seed in node_window or (prior.start is None and prior.end is None):
            continue
        owners = {res.terminal(node_contact[w.node]) for w in windows[prior.value]
                  if max(prior.start if prior.start is not None else -_INF,
                         w.start if w.start is not None else -_INF)
                  < min(prior.end if prior.end is not None else _INF,
                        w.end if w.end is not None else _INF)}
        if len(owners) == 1:
            contact = next(iter(owners))
            generated = str(uuid.uuid5(NAMESPACE, seed))
            if generated not in rows:
                c = rows[contact]
                rows[generated] = ContactRow(generated, c.kind, None, None, c.status, contact,
                                             _cap(set(c.source_refs) | prior.refs))
    res.contacts = sorted(rows.values(), key=lambda c: c.contact_id)
    # Consolidate equal output windows (e.g. a propagated slice and duplicate evidence).
    combined: dict[tuple[Any, ...], IdentRow] = {}
    for row in identifiers:
        key = (row.contact_id, row.kind, row.value, row.valid_from_ms, row.valid_until_ms)
        if key in combined:
            old = combined[key]
            row = replace(old, source_refs=_cap(set(old.source_refs) | set(row.source_refs)))
        combined[key] = row
    res.identifiers = list(combined.values())
    res.__post_init__()
    for value, items in windows.items():
        for n, a in enumerate(items):
            for b in items[n + 1:]:
                if (max(a.start if a.start is not None else -_INF, b.start if b.start is not None else -_INF)
                        < min(a.end if a.end is not None else _INF, b.end if b.end is not None else _INF)
                        and res.terminal(node_contact[a.node]) != res.terminal(node_contact[b.node])):
                    review["identifier_conflicts"].append({"identifier": value,
                        "source_refs": sorted(a.refs | b.refs)})
    for c in res.contacts:
        res.terminal(c.contact_id)
    return res
