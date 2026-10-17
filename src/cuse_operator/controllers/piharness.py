"""Per-user harness intent bound to a resolved WeeChat client."""
from copy import deepcopy
from .base import Controller, make_plan


def build_plan(body, context):
    spec = body["spec"]
    weechat = context.dependency("WeeChat", spec["weechatRef"])
    return make_plan(body, {"kind": "PiHarness", "spec": deepcopy(spec),
                           "userBinding": spec["user_id"], "dependencies": [weechat]})


controller = Controller("PiHarness", build_plan)
