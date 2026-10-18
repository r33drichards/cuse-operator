"""Bot desired pair of compatible WeeChat and harness identities."""
from copy import deepcopy
from .base import Controller, make_plan


def build_plan(body, context):
    spec = body["spec"]
    weechat = context.dependency("WeeChat", spec["weechatRef"])
    harness = context.dependency("PiHarness", spec["harnessRef"])
    return make_plan(body, {"kind": "Bot", "spec": deepcopy(spec),
                           "botIdentity": spec["id"], "dependencies": [weechat, harness]})


class BotController(Controller):
    async def reconcile(self, body, inventory, provider=None):
        result = await super().reconcile(body, inventory, provider)
        if result["ready"]:
            result["message"] = "WeeChat and harness runtime bindings are ready; message ingress is not managed."
        return result


controller = BotController("Bot", build_plan)
