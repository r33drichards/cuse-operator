"""Compatibility aggregate: validate and reconcile a snapshot, never split CRs.

This controller has no split-resource imports, creation, ownership or deletion.
Its provider intent is exclusively an aggregate snapshot.
"""
from ..codec import architecture_from_dict, to_dict
from .base import Controller, make_plan
from ..providers import DisabledProvider


def validate(body):
    architecture_from_dict(body.get("spec"))
    return {"phase": "Valid", "reason": "Validated", "message": "Aggregate configuration is valid."}


def build_plan(body, context):
    snapshot = to_dict(architecture_from_dict(body["spec"]))
    return make_plan(body, {"kind": "Architecture", "snapshot": snapshot, "dependencies": []})


class ArchitectureController(Controller):
    async def reconcile(self, body, inventory, provider=None):
        # Compatibility input must never expand into another managed graph,
        # even if an infrastructure provider was accidentally routed here.
        result = await super().reconcile(body, inventory, DisabledProvider())
        if result["phase"] == "Valid":
            result.update(ready=False, reconciliationReason="CompatibilityOnly",
                          message="Aggregate is valid compatibility input; manage standalone CRDs to run resources.")
        return result


controller = ArchitectureController("Architecture", build_plan, validate)
