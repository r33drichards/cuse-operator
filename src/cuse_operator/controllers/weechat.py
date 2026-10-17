"""WeeChat client binding to its resolved IRC server."""
from copy import deepcopy
from .base import Controller, make_plan


def build_plan(body, context):
    spec = body["spec"]
    server = context.dependency("IRCServer", spec["serverRef"])
    return make_plan(body, {"kind": "WeeChat", "spec": deepcopy(spec),
                           "client": {"nick": spec["nick"], "mcpEndpoint": spec["mcp_endpoint"]},
                           "dependencies": [server]})


controller = Controller("WeeChat", build_plan)
