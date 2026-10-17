"""Generate an embedded structural OpenAPI schema from canonical dataclasses.

The API group is provisional: this project makes no domain-ownership claim.
Unknown fields are pruned by Kubernetes; the Python codec rejects them before
submission. Do not add additionalProperties:false beside properties: that is
not an accepted CRD schema shape. Semantic links are checked by the reconciler.
"""
from copy import deepcopy
from dataclasses import MISSING, fields, is_dataclass
import argparse
import json
from pathlib import Path
from typing import get_args, get_origin, get_type_hints

from .models import Architecture

GROUP = "cuse.io"
VERSION = "v1alpha1"
PLURAL = "architectures"
KIND = "Architecture"


def schema_for(cls):
    """Return an inline structural schema; unsupported hints fail explicitly."""
    if hasattr(cls, "__supertype__"):
        return schema_for(cls.__supertype__)
    primitives = {str: "string", int: "integer", float: "number", bool: "boolean"}
    if cls in primitives:
        return {"type": primitives[cls]}
    if get_origin(cls) is tuple:
        args = get_args(cls)
        if len(args) != 2 or args[1] is not Ellipsis:
            raise TypeError("only homogeneous variable-length tuples are supported")
        return {"type": "array", "items": schema_for(args[0])}
    if not isinstance(cls, type) or not is_dataclass(cls):
        raise TypeError(f"unsupported schema type: {cls!r}")
    properties, required = {}, []
    hints = get_type_hints(cls)
    for field in fields(cls):
        child = schema_for(hints[field.name])
        if field.default is not MISSING:
            child["default"] = _json_default(field.default)
        elif field.default_factory is not MISSING:
            child["default"] = _json_default(field.default_factory())
        else:
            required.append(field.name)
        properties[field.name] = child
    result = {"type": "object", "properties": properties}
    if required:
        result["required"] = required
    return result


def _json_default(value):
    if is_dataclass(value):
        return {field.name: _json_default(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, tuple):
        return [_json_default(item) for item in value]
    return value


def status_schema():
    """Typed validation and provider convergence output belongs in status."""
    return {
        "type": "object",
        "properties": {
            "phase": {"type": "string", "enum": ["Valid", "Invalid"]},
            "observedGeneration": {"type": "integer", "minimum": 0},
            "reason": {"type": "string", "enum": ["Validated", "ValidationFailed", "Retained"]},
            "message": {"type": "string"},
            "ready": {"type": "boolean"},
            "reconciliationReason": {"type": "string"},
            "desiredRevision": {"type": "string"},
            "observedRevision": {"type": "string"},
            "lastAction": {"type": "string", "enum": ["None", "Create", "Update", "NoOp", "Retain"]},
        },
        "required": ["phase", "observedGeneration", "reason", "message"],
    }


def build_crd():
    """The aggregate's spec is the canonical serialized Architecture object."""
    return {
        "apiVersion": "apiextensions.k8s.io/v1",
        "kind": "CustomResourceDefinition",
        "metadata": {"name": f"{PLURAL}.{GROUP}"},
        "spec": {
            "group": GROUP,
            "scope": "Namespaced",
            "names": {"plural": PLURAL, "singular": "architecture", "kind": KIND},
            "versions": [{
                "name": VERSION, "served": True, "storage": True,
                "subresources": {"status": {}},
                "additionalPrinterColumns": [
                    {"name": "Validation", "type": "string", "jsonPath": ".status.phase"},
                    {"name": "Observed", "type": "integer", "jsonPath": ".status.observedGeneration"},
                ],
                "schema": {"openAPIV3Schema": {
                    "type": "object",
                    "properties": {
                        "apiVersion": {"type": "string"},
                        "kind": {"type": "string"},
                        "metadata": {"type": "object"},
                        "spec": schema_for(Architecture),
                        "status": status_schema(),
                    },
                    "required": ["spec"],
                }},
            }],
        },
    }


def structural_schema(schema):
    """Kubernetes prunes unknown fields; disallow additionalProperties:false."""
    if isinstance(schema, dict):
        return {key: structural_schema(value) for key, value in schema.items()
                if key != "additionalProperties"}
    if isinstance(schema, list):
        return [structural_schema(value) for value in schema]
    return schema


def resource_status_schema():
    result = status_schema()
    result["properties"]["phase"]["enum"].append("Blocked")
    result["properties"]["reason"]["enum"].extend([
        "MissingDependency", "ReferenceMismatch", "DuplicateIdentity",
    ])
    return result


def build_resource_crd(kind):
    """Build one independently addressable, namespaced resource definition."""
    from .resources import PLURALS, RESOURCE_SPECS

    result = deepcopy(build_crd())
    plural = PLURALS[kind]
    result["metadata"]["name"] = f"{plural}.{GROUP}"
    result["spec"]["names"] = {"plural": plural, "singular": kind.lower(), "kind": kind}
    schema = result["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
    schema["properties"]["spec"] = structural_schema(RESOURCE_SPECS[kind])
    schema["properties"]["status"] = resource_status_schema()
    return result


def build_crds():
    """Keep the aggregate compatibility API alongside seven independent kinds."""
    from .resources import PLURALS

    return {"apiVersion": "v1", "kind": "List", "items": [
        build_crd(), *(build_resource_crd(kind) for kind in PLURALS),
    ]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="write JSON to this file instead of stdout")
    parser.add_argument("--all", action="store_true", help="include all independent CRDs")
    args = parser.parse_args()
    document = json.dumps(build_crds() if args.all else build_crd(), indent=2) + "\n"
    if args.output:
        args.output.write_text(document, encoding="utf-8")
    else:
        print(document, end="")


if __name__ == "__main__":
    main()
