"""Compatibility aggregate: validate and reconcile a snapshot, never split CRs.

This controller has no split-resource imports, creation, ownership or deletion.
Its provider intent is exclusively an aggregate snapshot.
"""
from ..codec import architecture_from_dict, to_dict
from .base import Controller, make_plan


def validate(body):
    architecture_from_dict(body.get("spec"))
    return {"phase": "Valid", "reason": "Validated", "message": "Aggregate configuration is valid."}


def build_plan(body, context):
    snapshot = to_dict(architecture_from_dict(body["spec"]))
    return make_plan(body, {"kind": "Architecture", "snapshot": snapshot, "dependencies": []})


controller = Controller("Architecture", build_plan, validate)
