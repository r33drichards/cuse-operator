"""Offline validation entry point. Never prints input values or connects to a cluster."""
import argparse
import json
import sys
from pathlib import Path

from .codec import architecture_from_dict
from .validation import ValidationError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path, help="JSON domain object or Architecture resource")
    args = parser.parse_args(argv)
    try:
        value = json.loads(args.file.read_text())
        if isinstance(value, dict) and "apiVersion" in value:
            from .schema import GROUP, VERSION, KIND
            if value.get("apiVersion") != f"{GROUP}/{VERSION}" or value.get("kind") != KIND:
                raise ValueError("unsupported resource envelope")
            value = value.get("spec")
        architecture_from_dict(value)
    except (OSError, ValueError, ValidationError):
        # Neither paths supplied by callers nor bad configuration values are echoed.
        print("Invalid architecture. Use the Python validation API for structured diagnostics.", file=sys.stderr)
        return 1
    print("Valid architecture (offline validation only; runtime readiness not checked).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
