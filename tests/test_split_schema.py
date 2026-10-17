"""Independent CRDs preserve the aggregate API and Kubernetes schema shape."""
import json
from pathlib import Path

import jsonschema

from cuse_operator.resources import PLURALS
from cuse_operator.schema import build_crd, build_crds, resource_status_schema


def test_checked_in_all_crds_and_exact_kinds():
    document = build_crds()
    assert document == json.loads((Path(__file__).parents[1] / "config/crds.json").read_text())
    assert document["kind"] == "List"
    assert document["items"][0] == build_crd()
    assert {item["spec"]["names"]["kind"] for item in document["items"]} == {
        "Architecture", *PLURALS}
    for item in document["items"]:
        assert item["spec"]["scope"] == "Namespaced"
        version = item["spec"]["versions"][0]
        assert version["subresources"] == {"status": {}}
        schema = version["schema"]["openAPIV3Schema"]
        jsonschema.Draft7Validator.check_schema(schema)
        assert "additionalProperties" not in json.dumps(schema)
        assert "$ref" not in json.dumps(schema)
        assert "preserve-unknown" not in json.dumps(schema)


def test_dependency_status_has_explicit_blocked_reasons():
    jsonschema.validate({"phase": "Blocked", "reason": "MissingDependency",
                         "message": "A required dependency is missing or deleting.",
                         "observedGeneration": 2}, resource_status_schema())
