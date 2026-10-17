import json
from pathlib import Path

from cuse_operator.cli import main


def test_cli_example(capsys):
    assert main(["examples/architecture.json"]) == 0
    assert "offline" in capsys.readouterr().out


def test_cli_resource_envelope(tmp_path):
    example = json.loads(Path("examples/architecture.json").read_text())
    file = tmp_path / "domain.json"
    file.write_text(json.dumps({"apiVersion":"cuse.io/v1alpha1", "kind":"Architecture", "metadata":{"name":"example"}, "spec":example}))
    assert main([str(file)]) == 0


def test_cli_rejects_wrong_envelope(tmp_path, capsys):
    file = tmp_path / "bad.json"
    file.write_text(json.dumps({"apiVersion": "bad", "kind": "Architecture", "secret": "do-not-echo"}))
    assert main([str(file)]) == 1
    assert "do-not-echo" not in capsys.readouterr().err


def test_cli_bad_json_and_missing_file(tmp_path):
    file = tmp_path / "bad.json"
    file.write_text("invalid json")
    assert main([str(file)]) == 1
    assert main([str(tmp_path / "absent")]) == 1


def test_cli_split_inventory():
    assert main(["examples/resources.json"]) == 0


def test_cli_split_missing_dependency(tmp_path):
    value = json.loads(Path("examples/resources.json").read_text())
    value["items"] = value["items"][1:]
    file = tmp_path / "missing.json"
    file.write_text(json.dumps(value))
    assert main([str(file)]) == 1


def test_cli_duplicate_object_names(tmp_path):
    value = json.loads(Path("examples/resources.json").read_text())
    value["items"].append(value["items"][0])
    file = tmp_path / "duplicate.json"
    file.write_text(json.dumps(value))
    assert main([str(file)]) == 1


def test_cli_malformed_inventory_does_not_traceback(tmp_path):
    for item in [
        {"apiVersion": "cuse.io/v1alpha1", "kind": [], "metadata": {"name": "bad"}},
        {"apiVersion": "cuse.io/v1alpha1", "kind": "IRCServer", "metadata": []},
    ]:
        file = tmp_path / "bad-list.json"
        file.write_text(json.dumps({"apiVersion": "v1", "kind": "List", "items": [item]}))
        assert main([str(file)]) == 1


def test_cli_malformed_kind_does_not_traceback(tmp_path):
    file = tmp_path / "bad-kind.json"
    file.write_text(json.dumps({"apiVersion": "cuse.io/v1alpha1", "kind": []}))
    assert main([str(file)]) == 1
