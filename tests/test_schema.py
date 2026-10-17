"""Schema generation and CRD contract checks (no Kubernetes required)."""
import json
from pathlib import Path

import jsonschema
import pytest

from cuse_operator.codec import architecture_from_dict, to_dict
from cuse_operator.models import Architecture, MCPClient
from cuse_operator.schema import build_crd, schema_for, status_schema

ROOT = Path(__file__).resolve().parents[1]


def test_checked_in_crd_has_no_drift():
    assert json.loads((ROOT / "config/crd.json").read_text()) == build_crd()


def test_complete_example_matches_domain_and_embedded_schema():
    example = json.loads((ROOT / "examples/architecture.json").read_text())
    assert to_dict(architecture_from_dict(example)) == example
    schema = build_crd()["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
    jsonschema.Draft7Validator.check_schema(schema)
    jsonschema.validate({"spec": example}, schema)


def test_required_and_defaults_follow_dataclass_contract():
    schema = schema_for(Architecture)
    assert schema["required"] == ["irc"]
    assert schema["properties"]["bots"]["default"] == []
    assert schema["properties"]["bots"]["items"]["required"] == ["id", "weechat", "harness"]
    assert schema_for(MCPClient)["properties"]["connectors"]["default"] == []
    jsonschema.validate({"irc": {"id": "main", "endpoint": "ircs://irc.example.test"}}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"irc": {"id": 1, "endpoint": "ircs://irc.example.test"}}, schema)


def test_all_embedded_nodes_are_structural_and_typed():
    spec = schema_for(Architecture)

    def visit(node):
        assert "type" in node
        assert "$ref" not in node
        assert "x-kubernetes-preserve-unknown-fields" not in node
        assert "additionalProperties" not in node
        if node["type"] == "object":
            assert set(node.get("required", ())) <= set(node["properties"])
            for child in node["properties"].values():
                visit(child)
        if node["type"] == "array":
            visit(node["items"])
    visit(spec)


def test_single_namespaced_aggregate_and_status_subresource():
    crd = build_crd()
    assert crd["spec"]["scope"] == "Namespaced"
    assert crd["spec"]["names"]["kind"] == "Architecture"
    assert len(crd["spec"]["versions"]) == 1
    assert crd["spec"]["versions"][0]["subresources"] == {"status": {}}
    assert "kopf" not in status_schema()["properties"]


def test_unsupported_types_fail_instead_of_emitting_untyped_nodes():
    with pytest.raises(TypeError):
        schema_for(dict)
    with pytest.raises(TypeError):
        schema_for(tuple[str, int])
