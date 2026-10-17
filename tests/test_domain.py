"""Canonical domain contract, strict codec, and reference/security checks."""
import copy
from dataclasses import FrozenInstanceError
import unittest

from cuse_operator.codec import architecture_from_dict, from_dict, to_dict
from cuse_operator.models import Architecture, IRCServer, SessionId, UserId
from cuse_operator.validation import ValidationError, validate_architecture


def sample():
    return {
        "irc": {"id": "irc", "endpoint": "ircs://irc.example.test:6697"},
        "bots": [{
            "id": "bot", "weechat": {
                "id": "wc", "server_id": "irc", "nick": "dot",
                "mcp_endpoint": "http://weechat:8080/mcp",
            },
            "harness": {
                "id": "harness", "user_id": "user", "weechat_id": "wc",
                "sessions": [{
                    "id": "session", "harness_id": "harness",
                    "channel": {"server_id": "irc", "name": "#wchat"},
                    "context_ref": "s3://contexts/session.json",
                    "share_url": "https://example.test/session",
                    "mcp": {"id": "mcp", "endpoint": "http://mcp:8080", "connectors": [{
                        "id": "connector", "server_id": "irc", "nick": "dot-mcp",
                        "mcp_endpoint": "http://connector:8080/mcp",
                    }]},
                }],
            },
        }],
    }


class DomainTests(unittest.TestCase):
    def assert_invalid(self, value, path):
        with self.assertRaises(ValidationError) as caught:
            architecture_from_dict(value)
        self.assertEqual(caught.exception.path, path)
        self.assertEqual(caught.exception.errors[0].path, path)
        self.assertTrue(caught.exception.message)

    def test_round_trip_and_immutable(self):
        raw = sample()
        architecture = architecture_from_dict(raw)
        self.assertEqual(to_dict(architecture), raw)
        self.assertIsInstance(architecture.bots, tuple)
        self.assertIsInstance(architecture.bots[0].harness.sessions, tuple)
        self.assertFalse(hasattr(architecture, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            architecture.bots = ()
        self.assertIs(UserId.__supertype__, str)
        self.assertIs(SessionId.__supertype__, str)

    def test_optional_tuples_default(self):
        value = architecture_from_dict({"irc": sample()["irc"]})
        self.assertEqual(value.bots, ())
        self.assertEqual(to_dict(value)["bots"], [])

    def test_unknown_missing_wrong_types(self):
        for key, value, path in [
            ("extra", 1, "$.extra"), ("bots", {}, "$.bots"),
            ("bots", (), "$.bots"), ("irc", None, "$.irc"),
        ]:
            with self.subTest(key=key, value=value):
                raw = sample()
                raw[key] = value
                self.assert_invalid(raw, path)
        raw = sample()
        del raw["irc"]["id"]
        self.assert_invalid(raw, "$.irc.id")
        for value in [True, False, 1, None, [], {}]:
            raw = sample()
            raw["irc"]["id"] = value
            self.assert_invalid(raw, "$.irc.id")

    def test_runtime_constructors_do_not_bypass_validation(self):
        for value in [Architecture(IRCServer(True, "irc://host")), Architecture(IRCServer("irc", "irc://host"), [])]:
            with self.assertRaises(ValidationError):
                validate_architecture(value)
            with self.assertRaises(ValidationError):
                to_dict(value)

    def test_links_and_duplicates(self):
        raw = sample()
        raw["bots"][0]["harness"]["weechat_id"] = "missing"
        self.assert_invalid(raw, "$.bots[0].harness.weechat_id")
        raw = sample()
        raw["bots"][0]["harness"]["sessions"][0]["harness_id"] = "missing"
        self.assert_invalid(raw, "$.bots[0].harness.sessions[0].harness_id")
        raw = sample()
        raw["bots"][0]["weechat"]["server_id"] = "missing"
        raw["bots"][0]["harness"]["sessions"] = []
        self.assert_invalid(raw, "$.bots[0].weechat.server_id")
        raw = sample()
        raw["bots"].append(copy.deepcopy(raw["bots"][0]))
        self.assert_invalid(raw, "$.bots[1].id")
        for field in ["weechat", "harness"]:
            raw = sample()
            other = copy.deepcopy(raw["bots"][0])
            other["id"] = "second"
            if field == "harness":
                other["weechat"]["id"] = "second-wc"
                other["harness"]["weechat_id"] = "second-wc"
            raw["bots"].append(other)
            self.assert_invalid(raw, f"$.bots[1].{field}.id")
        raw = sample()
        sessions = raw["bots"][0]["harness"]["sessions"]
        sessions.append(copy.deepcopy(sessions[0]))
        self.assert_invalid(raw, "$.bots[0].harness.sessions[1].id")
        raw = sample()
        connectors = raw["bots"][0]["harness"]["sessions"][0]["mcp"]["connectors"]
        connectors[0]["server_id"] = "missing"
        self.assert_invalid(raw, "$.bots[0].harness.sessions[0].mcp.connectors[0].server_id")

    def test_url_policy(self):
        for endpoint in ["relative", "https://user:pass@host", "https://host/?api_key=secret", "https://host/#access_token=secret", "https://host:bad", "https://host:99999", "https://host/\npath", "https://user%40host", "https://host\\@other"]:
            with self.subTest(endpoint=endpoint):
                raw = sample()
                raw["irc"]["endpoint"] = endpoint
                self.assert_invalid(raw, "$.irc.endpoint")
        for endpoint in ["irc://localhost", "ircs://[::1]:6697", "custom+rpc://service/path?version=1"]:
            self.assertEqual(from_dict(IRCServer, {"id": "s", "endpoint": endpoint}).endpoint, endpoint)
        for reference in ["contexts/session.json", "file:///data/session.json", "s3://bucket/key", "urn:session:123"]:
            raw = sample()
            raw["bots"][0]["harness"]["sessions"][0]["context_ref"] = reference
            architecture_from_dict(raw)
        raw = sample()
        raw["bots"][0]["harness"]["sessions"][0]["context_ref"] = "s3://user:secret@bucket/key"
        self.assert_invalid(raw, "$.bots[0].harness.sessions[0].context_ref")

    def test_channel_rules(self):
        for channel in ["wchat", "#", "#with space", "#two,channels", "#bad:channel", "#bad\x07"]:
            raw = sample()
            raw["bots"][0]["harness"]["sessions"][0]["channel"]["name"] = channel
            self.assert_invalid(raw, "$.bots[0].harness.sessions[0].channel.name")
        for channel in ["#wchat", "&local", "+modeless", "!safe"]:
            raw = sample()
            raw["bots"][0]["harness"]["sessions"][0]["channel"]["name"] = channel
            architecture_from_dict(raw)

    def test_arbitrary_objects_are_not_codec_inputs(self):
        for cls in [str, int, dict]:
            with self.assertRaises(TypeError):
                from_dict(cls, {})
        with self.assertRaises(TypeError):
            to_dict({})


if __name__ == "__main__":
    unittest.main()
