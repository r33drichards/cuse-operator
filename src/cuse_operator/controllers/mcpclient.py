"""MCP endpoint intent with ordered, resolved connector bindings."""
from copy import deepcopy
from .base import Controller, make_plan


def build_plan(body, context):
    spec = body["spec"]
    connectors = [context.dependency("Connector", ref) for ref in spec.get("connectorRefs", [])]
    return make_plan(body, {"kind": "MCPClient", "spec": deepcopy(spec),
                           "endpoint": spec["endpoint"], "dependencies": connectors})


controller = Controller("MCPClient", build_plan)
