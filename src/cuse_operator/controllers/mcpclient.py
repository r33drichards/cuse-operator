"""MCP endpoint intent with ordered, resolved connector bindings."""
from copy import deepcopy
from .base import Controller, make_plan


def build_plan(body, context):
    spec = body["spec"]
    connectors = [context.dependency("Connector", ref) for ref in spec.get("connectorRefs", [])]
    downstreams = []
    for ref in spec.get("connectorRefs", []):
        connector = context.index[("Connector", body["metadata"]["namespace"], ref["name"])]["spec"]
        downstreams.append({"name": connector["id"], "url": connector["mcp_endpoint"]})
    return make_plan(body, {"kind": "MCPClient", "spec": deepcopy(spec),
                           "endpoint": spec["endpoint"], "downstreams": downstreams, "dependencies": connectors})


controller = Controller("MCPClient", build_plan)
