"""Offline validation entry point. Never prints input values or connects to a cluster."""
import argparse
import json
import sys
from pathlib import Path

from .codec import architecture_from_dict
from .validation import ValidationError


def validate_document(value):
    """Validate a bare Architecture, its resource envelope, or split resource List.

A standalone split resource with references is Blocked unless its dependencies
are supplied in a List. This is a local inventory, never a live-cluster lookup.
"""
    from .resources import PLURALS, validate_resource
    from .schema import GROUP, VERSION, KIND
    api_version = f"{GROUP}/{VERSION}"
    if not isinstance(value, dict):
        raise ValueError("expected object")
    if value.get("kind") == "List":
        if value.get("apiVersion") != "v1" or not isinstance(value.get("items"), list):
            raise ValueError("invalid List envelope")
        inventory = value["items"]
        for item in inventory:
            if not isinstance(item, dict) or item.get("apiVersion") != api_version:
                raise ValueError("invalid resource envelope")
        keys = set()
        for item in inventory:
            kind = item.get("kind")
            if not isinstance(kind, str):
                raise ValueError("invalid resource kind")
            metadata = item.get("metadata", {})
            if not isinstance(metadata, dict):
                raise ValueError("invalid metadata")
            name = metadata.get("name")
            namespace = metadata.get("namespace", "default")
            if not isinstance(name, str) or not name or not isinstance(namespace, str) or not namespace:
                raise ValueError("invalid identity")
            key = (kind, namespace, name)
            if key in keys:
                raise ValueError("duplicate resource identity")
            keys.add(key)
            if kind == KIND:
                architecture_from_dict(item.get("spec"))
            elif kind in PLURALS:
                if any(not isinstance(obj.get("metadata", {}), dict) for obj in inventory):
                    raise ValueError("invalid metadata")
                # Kubernetes supplies namespace; normalize omitted namespaces in local input.
                normalized = [dict(obj, metadata=dict(obj.get("metadata", {}), namespace=obj.get("metadata", {}).get("namespace", "default"))) for obj in inventory]
                result = validate_resource(kind, item.get("spec"), namespace, normalized)
                if result["phase"] != "Valid":
                    raise ValueError("invalid or unresolved resource")
            else:
                raise ValueError("unsupported resource kind")
        return
    if "apiVersion" in value:
        if not isinstance(value.get("kind"), str):
            raise ValueError("invalid resource kind")
        if value.get("apiVersion") != api_version:
            raise ValueError("unsupported resource API")
        if value.get("kind") == KIND:
            architecture_from_dict(value.get("spec"))
        elif value.get("kind") in PLURALS:
            validate_document({"apiVersion": "v1", "kind": "List", "items": [value]})
        else:
            raise ValueError("unsupported resource kind")
    else:
        architecture_from_dict(value)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path, help="JSON domain object, resource or resource List")
    args = parser.parse_args(argv)
    try:
        validate_document(json.loads(args.file.read_text()))
    except (OSError, ValueError, ValidationError):
        print("Invalid or unresolved configuration. Use the Python validation API for diagnostics.", file=sys.stderr)
        return 1
    print("Valid configuration (offline validation only; runtime readiness not checked).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
