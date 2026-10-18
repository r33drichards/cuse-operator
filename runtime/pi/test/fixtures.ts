import {
  canonical,
  sha256,
  type Desired,
  type Identity,
  type Intent,
} from "../src/contract.ts";
export const AUTH = "Bearer local-control-test-token-never-production";
const digest = "a".repeat(64);
export const identity = (
  kind: "PiHarness" | "Session",
  suffix: string,
): Identity => ({
  kind,
  namespace: "tests",
  name: `resource-${suffix}`,
  uid: `uid-${suffix}`,
});
const dependency = (
  kind: string,
  name: string,
  uid: string,
  revision = digest,
) => ({
  kind,
  namespace: "tests",
  name,
  uid,
  specDigest: digest,
  desiredRevision: revision,
});
export async function desired(
  id: Identity,
  intent: Intent,
  expected_revision: string | null = null,
): Promise<Desired> {
  const revision = await sha256(canonical(intent));
  return {
    identity: id,
    intent,
    revision,
    idempotency_key: await sha256(
      canonical([id.kind, id.namespace, id.name, id.uid, revision]),
    ),
    expected_revision,
  };
}
export function harnessIntent(suffix: string): Intent {
  return {
    kind: "PiHarness",
    spec: {
      id: `harness-${suffix}`,
      user_id: `user-${suffix}`,
      weechatRef: { name: "weechat" },
    },
    userBinding: `user-${suffix}`,
    dependencies: [dependency("WeeChat", "weechat", "weechat-uid")],
  };
}
export function sessionIntent(suffix: string, parent: Desired): Intent {
  return {
    kind: "Session",
    spec: {
      id: `session-${suffix}`,
      harnessRef: { name: parent.identity.name, uid: parent.identity.uid },
      channel: { serverRef: { name: "irc" }, name: `#${suffix}` },
      context_ref: `context:${suffix}`,
      share_url: `https://share.example.invalid/${suffix}`,
      mcpClientRef: { name: "mcp" },
    },
    sessionContext: {
      channel: `#${suffix}`,
      contextRef: `context:${suffix}`,
      shareURL: `https://share.example.invalid/${suffix}`,
    },
    dependencies: [
      dependency(
        "PiHarness",
        parent.identity.name,
        parent.identity.uid,
        parent.revision,
      ),
      dependency("IRCServer", "irc", "irc-uid"),
      dependency("MCPClient", "mcp", "mcp-uid"),
    ],
  };
}
