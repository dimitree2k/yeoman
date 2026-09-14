"""Strict validation against the pinned Hermes/Yeoman A2A contract."""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from typing import Any

import a2a_contracts
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

CONTRACT_RELEASE = "1.0.1"
CONTRACT_COMMIT = "1c658bfc4d5ae872f47d9abeef5aa365cf305af1"
PROFILE_URI = "urn:hermes-yeoman:a2a-profile:v1"

#: Callers act on these messages (a model retries with a corrected payload, an operator reads
#: the rejection in a log), so the message names the offending fields instead of only the JSON
#: Schema keyword. Bounded because a payload may violate the schema in arbitrarily many places.
_DETAIL_NAME_LIMIT = 8
_DETAIL_LENGTH_LIMIT = 480

if a2a_contracts.CONTRACT_VERSION != CONTRACT_RELEASE:
    raise RuntimeError(
        f"contract release mismatch: expected {CONTRACT_RELEASE}, "
        f"installed {a2a_contracts.CONTRACT_VERSION}"
    )
if a2a_contracts.PROFILE_URI != PROFILE_URI:
    raise RuntimeError(
        f"contract profile mismatch: expected {PROFILE_URI}, installed {a2a_contracts.PROFILE_URI}"
    )


class A2AContractValidationError(ValueError):
    """A contract payload failed schema validation.

    The message is bounded and names the offending fields where the schema allows it: callers
    either retry with a corrected payload or read the rejection in a log, so a bare JSON Schema
    keyword is not useful.
    """

    def __init__(self, path: str, reason: str, *, detail: str = "") -> None:
        self.path = path
        self.reason = reason
        self.detail = detail
        message = f"{path}: {reason}" + (f" ({detail})" if detail else "")
        if len(message) > _DETAIL_LENGTH_LIMIT:
            message = message[: _DETAIL_LENGTH_LIMIT - 1].rstrip() + "…"
        super().__init__(message)


def _bounded_names(values: list[str]) -> str:
    """Render offending field names, bounded so one payload cannot produce a wall of text."""
    shown = values[:_DETAIL_NAME_LIMIT]
    rendered = ", ".join(shown)
    if len(values) > len(shown):
        rendered += f", +{len(values) - len(shown)} more"
    return rendered


def _error_location(error: Any) -> str:
    return "$" + "".join(
        f".{part}" if isinstance(part, str) else f"[{part}]" for part in error.path
    )


def _missing_fields(error: Any) -> list[str]:
    """Required keys absent from the instance.

    jsonschema reports every key in the schema's ``required`` list, whether or not it is present,
    so the list is narrowed to the keys the payload actually omits.
    """
    instance = error.instance if isinstance(error.instance, dict) else {}
    return sorted(key for key in error.validator_value if key not in instance)


def _error_detail(error: Any) -> str:
    """Name the missing and unexpected fields instead of only the violated keyword.

    The field list wins over the raw validator message when the budget is tight: a caller acts
    on which field was wrong, and a payload can carry arbitrarily many unknown fields.
    """
    if error.validator == "required":
        missing = _missing_fields(error)
        if missing:
            return f"missing required fields: {_bounded_names(missing)}"
    elif error.validator == "additionalProperties":
        unexpected = sorted(set(error.instance) - set(error.schema.get("properties", {})))
        return f"unexpected fields: {_bounded_names(unexpected)}"
    return str(error.message)[:_DETAIL_LENGTH_LIMIT]


def _schema_files() -> list[tuple[str, dict[str, Any]]]:
    root = resources.files("a2a_contracts").joinpath("schemas")
    schemas: list[tuple[str, dict[str, Any]]] = []
    for path in root.rglob("*.json"):
        with path.open(encoding="utf-8") as handle:
            schemas.append((str(path.relative_to(root)), json.load(handle)))
    return schemas


@dataclass(frozen=True)
class ContractSchemas:
    _registry: Registry
    _schemas: dict[str, dict[str, Any]]

    @classmethod
    def load(cls) -> "ContractSchemas":
        loaded = _schema_files()
        registry = Registry()
        schemas: dict[str, dict[str, Any]] = {}
        for relative, schema in loaded:
            schema_id = schema.get("$id")
            if not isinstance(schema_id, str):
                raise RuntimeError(f"contract schema has no $id: {relative}")
            registry = registry.with_resource(schema_id, Resource.from_contents(schema))
            schemas[relative] = schema
        return cls(registry, schemas)

    def _validate(self, schema: dict[str, Any], value: Any) -> None:
        validator = Draft202012Validator(
            schema, registry=self._registry, format_checker=FormatChecker()
        )
        errors = sorted(validator.iter_errors(value), key=lambda item: list(item.path))
        if not errors:
            return
        error = errors[0]
        path = _error_location(error)
        # Several independent violations of the same object (a missing key plus unknown fields)
        # are one actionable failure for the caller; report them together.
        same_location = [item for item in errors if list(item.path) == list(error.path)]
        detail = " | ".join(
            part
            for part in (_error_detail(item) for item in same_location)
            if part
        )
        raise A2AContractValidationError(path, error.validator or error.message, detail=detail)

    def _skill_schema(self, skill: str, kind: str) -> dict[str, Any]:
        relative = f"skills/{skill}/{kind}.schema.json"
        schema = self._schemas.get(relative)
        if schema is None:
            raise A2AContractValidationError("$.skill", "unknown skill")
        return schema

    def validate_invocation(self, value: Any) -> None:
        self._validate(self._schemas["common/invocation.schema.json"], value)

    def validate_request(self, skill: str, value: Any) -> None:
        self._validate(self._skill_schema(skill, "request"), value)

    def validate_response(self, skill: str, value: Any) -> None:
        self._validate(self._skill_schema(skill, "response"), value)

    def validate_result(self, value: Any) -> None:
        self._validate(self._schemas["common/result.schema.json"], value)


__all__ = [
    "A2AContractValidationError",
    "CONTRACT_COMMIT",
    "CONTRACT_RELEASE",
    "PROFILE_URI",
    "ContractSchemas",
]
