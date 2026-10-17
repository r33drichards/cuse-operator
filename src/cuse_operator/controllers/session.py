"""Session context, channel, harness and MCP-client desired bindings."""
from copy import deepcopy
from .base import Controller, make_plan


def build_plan(body, context):
    spec = body["spec"]
    harness = context.dependency("PiHarness", spec["harnessRef"])
    server = context.dependency("IRCServer", spec["channel"]["serverRef"])
    client = context.dependency("MCPClient", spec["mcpClientRef"])
    return make_plan(body, {"kind": "Session", "spec": deepcopy(spec),
                           "sessionContext": {"channel": spec["channel"]["name"],
                                              "contextRef": spec["context_ref"], "shareURL": spec["share_url"]},
                           "dependencies": [harness, server, client]})


controller = Controller("Session", build_plan)
