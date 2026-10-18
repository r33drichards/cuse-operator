"""Downstream MCP server binding for mcp-js, retaining canonical IRC identity."""
from copy import deepcopy
from .base import Controller, make_plan


def build_plan(body, context):
    spec = body["spec"]
    server = context.dependency("IRCServer", spec["serverRef"])
    dependencies = [server]
    if "weechatRef" in spec:
        dependencies.append(context.dependency("WeeChat", spec["weechatRef"]))
    return make_plan(body, {"kind": "Connector", "spec": deepcopy(spec),
                           "downstream": {"name": spec["id"], "url": spec["mcp_endpoint"]},
                           "dependencies": dependencies})


controller = Controller("Connector", build_plan)
