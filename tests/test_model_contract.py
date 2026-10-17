from dataclasses import fields, MISSING

from cuse_operator import models


def test_exact_canonical_contract():
    expected = {
        "IRCServer": ["id", "endpoint"],
        "ChannelRef": ["server_id", "name"],
        "WeeChat": ["id", "server_id", "nick", "mcp_endpoint"],
        "Connector": ["id", "server_id", "nick", "mcp_endpoint"],
        "MCPClient": ["id", "endpoint", "connectors"],
        "Session": ["id", "harness_id", "channel", "context_ref", "share_url", "mcp"],
        "PiHarness": ["id", "user_id", "weechat_id", "sessions"],
        "Bot": ["id", "weechat", "harness"],
        "Architecture": ["irc", "bots"],
    }
    for name, names in expected.items():
        cls = getattr(models, name)
        assert [f.name for f in fields(cls)] == names
        assert cls.__dataclass_params__.frozen
        assert list(cls.__slots__) == names
        for field in fields(cls):
            if field.name in {"connectors", "sessions", "bots"}:
                assert field.default == ()
            else:
                assert field.default is MISSING
            assert field.default_factory is MISSING
