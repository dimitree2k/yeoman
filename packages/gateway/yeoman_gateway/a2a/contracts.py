"""Strict validation against the pinned Hermes/Yeoman A2A contract."""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from typing import Any

import a2a_contracts
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

CONTRACT_RELEASE = "1.0.0"
CONTRACT_COMMIT = "e408cc3d10cc9c76a245d851d7abfa2027874d51"
PROFILE_URI = "urn:hermes-yeoman:a2a-profile:v1"

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
    """A contract payload failed schema validation."""

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}")


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
        error = next(
            iter(sorted(validator.iter_errors(value), key=lambda item: list(item.path))), None
        )
        if error is None:
            return
        path = "$" + "".join(
            f".{part}" if isinstance(part, str) else f"[{part}]" for part in error.path
        )
        raise A2AContractValidationError(path, error.validator or error.message)

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
