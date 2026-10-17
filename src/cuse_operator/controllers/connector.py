"""IRC-to-MCP connector intent bound to the resolved server identity."""
from copy import deepcopy
from .base import Controller, make_plan


def build_plan(body, context):
    spec = body["spec"]
    server = context.dependency("IRCServer", spec["serverRef"])
    return make_plan(body, {"kind": "Connector", "spec": deepcopy(spec),
                           "bridge": {"nick": spec["nick"], "mcpEndpoint": spec["mcp_endpoint"]},
                           "dependencies": [server]})


controller = Controller("Connector", build_plan)
