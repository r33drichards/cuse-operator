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
