"""WeeChat client binding to its resolved IRC server."""
from copy import deepcopy
from .base import Controller, make_plan


def build_plan(body, context):
    spec = body["spec"]
    server = context.dependency("IRCServer", spec["serverRef"])
    server_spec = context.index[("IRCServer", body["metadata"]["namespace"], spec["serverRef"]["name"])]["spec"]
    return make_plan(body, {"kind": "WeeChat", "spec": deepcopy(spec),
                           "client": {"nick": spec["nick"], "mcpEndpoint": spec["mcp_endpoint"]},
                           "server": deepcopy(server_spec),
                           "dependencies": [server]})


controller = Controller("WeeChat", build_plan)
