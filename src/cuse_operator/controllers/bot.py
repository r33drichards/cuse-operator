"""Bot desired pair of compatible WeeChat and harness identities."""
from copy import deepcopy
from .base import Controller, make_plan


def build_plan(body, context):
    spec = body["spec"]
    weechat = context.dependency("WeeChat", spec["weechatRef"])
    harness = context.dependency("PiHarness", spec["harnessRef"])
    return make_plan(body, {"kind": "Bot", "spec": deepcopy(spec),
                           "botIdentity": spec["id"], "dependencies": [weechat, harness]})


controller = Controller("Bot", build_plan)
