"""Offline adapter tests; these deliberately perform no cluster operations."""
from copy import deepcopy
import json
from pathlib import Path

import jsonschema
import kopf
import pytest

from cuse_operator.operator import configure, reconcile, validate
from cuse_operator.schema import status_schema

ROOT = Path(__file__).resolve().parents[1]


def example():
    return json.loads((ROOT / "examples/architecture.json").read_text())


def test_reconciliation_is_pure_repeatable_and_typed():
    spec = example()
    original = deepcopy(spec)
    result = reconcile(spec, generation=3)
    assert result == reconcile(spec, generation=3)
    assert spec == original
    assert result["phase"] == "Valid"
    assert result["observedGeneration"] == 3
    jsonschema.validate(result, status_schema())


@pytest.mark.parametrize("spec", [None, [], {}, {"irc": None}, {"secret-input-value": "secret"}])
def test_malformed_inputs_produce_invalid_status_without_retry(spec):
    result = reconcile(spec, generation=4)
    assert result["phase"] == "Invalid"
    assert result["observedGeneration"] == 4
    assert "secret" not in json.dumps(result)
    jsonschema.validate(result, status_schema())


def test_cross_reference_and_credentials_are_redacted():
    spec = example()
    spec["bots"][0]["harness"]["weechat_id"] = "unknown-sensitive-value"
    assert reconcile(spec)["phase"] == "Invalid"
    assert "unknown-sensitive-value" not in json.dumps(reconcile(spec))
    spec = example()
    spec["irc"]["endpoint"] = "ircs://user:secret-value@irc.example.test"
    assert reconcile(spec)["phase"] == "Invalid"
    assert "secret-value" not in json.dumps(reconcile(spec))


def test_handler_only_patches_status_and_returns_none():
    patch = kopf.Patch()
    result = validate(spec=example(), meta={"generation": 9}, patch=patch)
    assert result is None
    assert set(patch) == {"status"}
    assert patch["status"] == reconcile(example(), generation=9)
    assert "finalizers" not in json.dumps(patch)


def test_invalid_handler_is_completed_validation_not_exception():
    patch = kopf.Patch()
    assert validate(spec={}, meta={"generation": 10}, patch=patch) is None
    assert patch["status"]["phase"] == "Invalid"


def test_framework_uses_annotations_and_disables_unneeded_permissions():
    settings = kopf.OperatorSettings()
    configure(settings)
    assert isinstance(settings.persistence.progress_storage, kopf.AnnotationsProgressStorage)
    assert isinstance(settings.persistence.diffbase_storage, kopf.AnnotationsDiffBaseStorage)
    assert settings.posting.enabled is False
    assert settings.scanning.disabled is True


def test_rbac_matches_validation_only_mode():
    import yaml

    documents = list(yaml.safe_load_all((ROOT / "config/rbac.yaml").read_text()))
    assert [document["kind"] for document in documents] == ["ServiceAccount", "Role", "RoleBinding"]
    role = documents[1]
    assert role["rules"] == [
        {"apiGroups": ["cuse.io"], "resources": ["architectures"], "verbs": ["get", "list", "watch", "patch"]},
        {"apiGroups": ["cuse.io"], "resources": ["architectures/status"], "verbs": ["patch"]},
    ]
    assert all(document["metadata"]["namespace"] == "default" for document in documents)
    assert documents[2]["subjects"][0]["name"] == documents[0]["metadata"]["name"]
