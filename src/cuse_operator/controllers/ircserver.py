"""IRC server endpoint intent and lifecycle reconciliation."""
from copy import deepcopy
from .base import Controller, make_plan


def build_plan(body, context):
    spec = body["spec"]
    return make_plan(body, {"kind": "IRCServer", "spec": deepcopy(spec),
                           "server": {"id": spec["id"], "endpoint": spec["endpoint"]}, "dependencies": []})


controller = Controller("IRCServer", build_plan)
