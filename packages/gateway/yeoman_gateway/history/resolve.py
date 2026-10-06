"""Identity resolution: one contact per person (spec: Identity resolution, steps 1-7)."""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
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


@dataclass
class IdentityInput:
    sightings: dict[Ident, Sighting] = field(default_factory=dict)
    groups: set[Ident] = field(default_factory=set)
    links: list[tuple[str, str, str, str]] = field(default_factory=list)
    names: dict[str, list[tuple[int | None, str, str]]] = field(default_factory=lambda: defaultdict(list))
    contact_records: dict[str, ContactRecord] = field(default_factory=dict)
    attestations: list[Attestation] = field(default_factory=list)

    def see(self, ident: Ident | None, ms: int | None, ref: str) -> None:
        if ident is None:
            return
        if ident.kind == "group":
            self.groups.add(ident)
        elif ident.kind in _NODE_KINDS:
            self.sightings.setdefault(ident, Sighting()).add(ms, ref)

    def link(self, a: Ident | None, b: Ident | None, evidence: str, ref: str) -> None:
        if a is None or b is None or not (a.strong and b.strong) or a == b:
            return
        self.see(a, None, ref)
        self.see(b, None, ref)
        self.links.append((a.value, b.value, evidence, ref))

    def bind(self, contact_ref: str, ident: Ident | None, ref: str) -> None:
        if ident is None or not ident.strong:
            return
        self.see(ident, None, ref)
        self.links.append((REF + contact_ref, ident.value, "knowledge_binding", ref))

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


@dataclass
class Resolution:
    contacts: list[ContactRow]
    identifiers: list[IdentRow]
    node_contact: dict[str, str]
    role_contact: dict[str, str]
    canonical: dict[str, Ident]
    review: dict[str, list[dict[str, Any]]]

    def resolve(self, ident: Ident | None) -> tuple[str | None, str]:
        if ident is None:
            return None, "unknown"
        if ident.kind == "group":
            return None, "group"
        if ident.kind == "assistant":
            return self.role_contact.get("assistant"), "exact"
        if ident.kind == "numeric":
            mapped = self.canonical.get(ident.value)
            if mapped is not None:
                if mapped.kind == "group":
                    return None, "group"
                return self.node_contact.get(mapped.value), "numeric_match"
            return self.node_contact.get(ident.value), "exact"
        if ident.strong:
            return self.node_contact.get(ident.value), "exact"
        return None, "unknown"

    def contact_for_anchor(self, value: Any) -> str | None:
        ident = classify(value)
        return None if ident is None else self.node_contact.get(ident.value)


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


def resolve(inp: IdentityInput) -> Resolution:
    review: dict[str, list[dict[str, Any]]] = {
        "numeric_ambiguous": [], "merged_knowledge_contacts": [], "blocked_by_unmerge": [],
        "identifier_ended_not_applied": []}
    attested = sorted(inp.attestations, key=lambda a: (a.at_ms, a.ref))

    strong_known = {i for i in inp.sightings if i.strong}
    for a, b, _, _ in inp.links:
        strong_known |= {i for i in (classify(a), classify(b)) if i is not None and i.strong}
    for att in attested:
        strong_known |= {classify(v) for v in _identifier_values(att)}
    index: dict[str, set[Ident]] = defaultdict(set)
    for ident in strong_known | inp.groups:
        index[numeric_part(ident.value)].add(ident)

    sightings: dict[Ident, Sighting] = {i: Sighting() for i in strong_known}
    for ident, seen in inp.sightings.items():
        if ident.strong:
            sightings[ident].merge(seen)
    names: dict[str, list[tuple[int | None, str, str]]] = defaultdict(list)
    for node, items in inp.names.items():
        names[node].extend(items)
    evidence_of: dict[str, set[str]] = defaultdict(set)
    refs_of: dict[str, set[str | None]] = defaultdict(set)
    for ident in inp.sightings:
        if ident.strong:
            evidence_of[ident.value].add("observed")

    canonical: dict[str, Ident] = {}
    for ident in sorted(i for i in inp.sightings if i.kind == "numeric"):
        candidates = index.get(ident.value, set())
        if len(candidates) == 1:
            target = next(iter(candidates))
            canonical[ident.value] = target
            if target.strong:
                sightings[target].merge(inp.sightings[ident])
                evidence_of[target.value].add("numeric_match")
                names[target.value].extend(names.pop(ident.value, []))
            continue
        if len(candidates) > 1:
            review["numeric_ambiguous"].append(
                {"value": ident.value, "candidates": sorted(c.value for c in candidates)})
        sightings[ident] = inp.sightings[ident]
    node_ident = {i.value: i for i in sightings}

    uf = _UnionFind()
    for node in node_ident:
        uf.add(node)
    for contact_ref in inp.contact_records:
        uf.add(REF + contact_ref)
    pair_intent: dict[tuple[str, str], Attestation] = {}
    for att in attested:
        if att.type in ("merge", "unmerge"):
            pair = tuple(sorted((_value(att.fields["a"]), _value(att.fields["b"]))))
            pair_intent[pair] = att
    active_merge_refs = {att.ref for att in pair_intent.values() if att.type == "merge"}
    forbidden = [pair for pair, att in pair_intent.items() if att.type == "unmerge"]
    edges: list[tuple[int, int, str, str, str, str]] = []
    for att in attested:
        values = [_value(v) for v in _identifier_values(att)]
        for value in values:
            evidence_of[value].add("owner_attested")
            refs_of[value].add(att.ref)
        if att.type == "contact":
            edges += [(0, att.at_ms, att.ref, values[0], v, "owner_attested") for v in values[1:]]
        elif att.type == "identifier" or (att.type == "merge" and att.ref in active_merge_refs):
            edges.append((0, att.at_ms, att.ref, values[0], values[1], "owner_attested"))
    for a, b, evidence, ref in inp.links:
        edges.append((_EDGE_PRIORITY[evidence], 0, ref, a, b, evidence))
    for _, _, ref, a, b, evidence in sorted(edges):
        if uf.union(a, b, forbidden):
            for node in (a, b):
                evidence_of[node].add(evidence)
                refs_of[node].add(ref)
        else:
            review["blocked_by_unmerge"].append({"a": a, "b": b, "evidence": evidence, "ref": ref})

    role_by_root: dict[str, str] = {}
    name_by_root: dict[str, str] = {}
    ended: dict[str, int] = {}
    for att in attested:
        if att.type == "contact":
            root = uf.find(_value(att.fields["identifiers"][0]))
            if att.fields.get("role"):
                role_by_root[root] = att.fields["role"]
            if att.fields.get("name"):
                name_by_root[root] = att.fields["name"]
        elif att.type == "name":
            name_by_root[uf.find(_value(att.fields["anchor"]))] = att.fields["name"]
        elif att.type == "identifier_ended":
            ended[_value(att.fields["identifier"])] = att.fields["ended_ms"]
            review["identifier_ended_not_applied"].append({
                "ref": att.ref, "identifier": _value(att.fields["identifier"]),
                "ended_ms": att.fields["ended_ms"], "resolution": "not applied to resolution"})

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
        idents = [node_ident[m] for m in members if m in node_ident]
        strong = [i for i in idents if i.strong]
        records = [inp.contact_records[r] for r in refs if r in inp.contact_records]
        if refs:
            chosen = min(refs, key=lambda r: (
                inp.contact_records[r].created_ms if r in inp.contact_records
                and inp.contact_records[r].created_ms is not None else _INF, r))
        elif strong:
            first = min(strong, key=lambda i: (
                sightings[i].first_ms if sightings[i].first_ms is not None else _INF, i.value))
            chosen = str(uuid.uuid5(NAMESPACE, first.value))
        else:
            chosen = str(uuid.uuid5(NAMESPACE, "numeric:" + idents[0].value))
        for member in members:
            node_contact[member] = chosen
        role = role_by_root.get(root)
        if role:
            role_contact[role] = chosen

        push: dict[str, Sighting] = {}
        for member in members:
            for ms, name, ref in names.get(member, []):
                push.setdefault(name, Sighting()).add(ms, ref)
        latest_push = max(push, key=lambda n: (push[n].last_ms or -1, n)) if push else None
        record = inp.contact_records.get(chosen)
        display = (name_by_root.get(root) or (record.preferred_name if record else None)
                   or (record.display_name if record else None) or latest_push)
        attested_here = any("owner_attested" in evidence_of[m] for m in members)
        status = "confirmed" if strong or refs or attested_here else "provisional"
        kind = "channel" if any(i.kind == "newsletter" for i in idents) else "person"
        contact_refs: set[str | None] = {r.ref for r in records}
        for member in members:
            contact_refs |= refs_of[member]
        for ident in idents:
            contact_refs.add(sightings[ident].first_ref)
        contacts.append(ContactRow(chosen, kind, role, display, status, None, _cap(contact_refs)))
        for other in sorted(r for r in refs if r != chosen):
            rec = inp.contact_records.get(other)
            contacts.append(ContactRow(other, kind, None, rec.display_name if rec else None, "confirmed",
                                       chosen, _cap({rec.ref} if rec else set())))
        if len(refs) > 1:
            review["merged_knowledge_contacts"].append({
                "contact_id": chosen, "merged": sorted(r for r in refs if r != chosen),
                "names": sorted({r.display_name for r in records if r.display_name})})
        for ident in idents:
            seen = sightings[ident]
            present = evidence_of[ident.value]
            evidence = next((e for e in _EVIDENCE_ORDER if e in present), "observed")
            identifiers.append(IdentRow(
                chosen, "whatsapp", ident.kind, ident.value, "strong" if ident.strong else "weak",
                evidence, seen.first_ms, seen.last_ms, ended.get(ident.value),
                _cap(refs_of[ident.value] | {seen.first_ref})))
        for name in sorted(push):
            seen = push[name]
            identifiers.append(IdentRow(chosen, "whatsapp", "push_name", name, "weak", "observed",
                                        seen.first_ms, seen.last_ms, None, _cap({seen.first_ref})))
    return Resolution(contacts, identifiers, node_contact, role_contact, canonical, review)
