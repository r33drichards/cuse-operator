import { env } from "cloudflare:workers";
import {
  evictDurableObject,
  runInDurableObject,
  runDurableObjectAlarm,
  SELF,
} from "cloudflare:test";
import { beforeAll, afterAll, vi, describe, expect, it } from "vitest";
import {
  AUTH,
  desired,
  harnessIntent,
  identity,
  sessionIntent,
} from "./fixtures.ts";
import {
  canonical,
  resourceName,
  resourcePath,
  sha256,
  type Desired,
} from "../src/contract.ts";
import pythonPlans from "./python-plans.json";
import type { Env } from "../src/config.ts";
const bindings = env as unknown as Env;
const request = async (
  path: string,
  method = "GET",
  body?: unknown,
  authorization = AUTH,
) => {
  const response = await SELF.fetch(`https://local.test${path}`, {
    method,
    headers: {
      Authorization: authorization,
      "Content-Type": "application/json",
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  return new Response(await response.text(), {
    status: response.status,
    headers: response.headers,
  });
};
const put = (d: Desired) => request(resourcePath(d.identity), "PUT", d);
async function seed(suffix: string) {
  const parent = await desired(
    identity("PiHarness", `h-${suffix}`),
    harnessIntent(suffix),
  );
  expect((await put(parent)).status).toBe(201);
  const child = await desired(
    identity("Session", `s-${suffix}`),
    sessionIntent(suffix, parent),
  );
  return { parent, child };
}
beforeAll(() => {
  vi.spyOn(globalThis, "fetch").mockRejectedValue(
    new Error("External network disabled in local tests"),
  );
});
afterAll(() => {
  vi.restoreAllMocks();
});
describe("authenticated UID-scoped control", () => {
  it("authenticates even unknown and destructive paths", async () => {
    expect((await request("/", "GET", undefined, "")).status).toBe(401);
    expect((await request("/", "GET", undefined, "Bearer wrong")).status).toBe(
      401,
    );
    expect(
      (await request(resourcePath(identity("Session", "no-delete")), "DELETE"))
        .status,
    ).toBe(405);
    expect((await request("/")).status).toBe(404);
  });
  it("persists PiHarness config but never conflates it with model connectivity", async () => {
    const d = await desired(
      identity("PiHarness", "configured"),
      harnessIntent("configured"),
    );
    expect((await request(resourcePath(d.identity))).status).toBe(404);
    const response = await put(d);
    expect(response.status).toBe(201);
    expect(await response.json()).toMatchObject({
      exists: true,
      ready: true,
      revision: d.revision,
      phase: "Configured",
    });
    await evictDurableObject(
      bindings.RESOURCES.getByName(resourceName(d.identity)),
    );
    expect(
      await (await request(resourcePath(d.identity))).json(),
    ).toMatchObject({ ready: true, revision: d.revision });
  });
  it("deduplicates replay, rejects stale CAS, and cannot roll back a newer revision", async () => {
    const d = await desired(identity("PiHarness", "cas"), harnessIntent("cas"));
    await put(d);
    expect((await put(d)).status).toBe(200);
    const changed = structuredClone(d.intent);
    changed.dependencies[0].desiredRevision = "b".repeat(64);
    const update = await desired(d.identity, changed);
    expect((await put(update)).status).toBe(409);
    update.expected_revision = d.revision;
    expect((await put(update)).status).toBe(200);
    expect(await (await put(d)).json()).toMatchObject({
      revision: update.revision,
    });
  });
  it("permits explicit A/B/A reverts while stale original replay cannot roll B back", async () => {
    const a = await desired(
      identity("PiHarness", "revert"),
      harnessIntent("revert"),
    );
    await put(a);
    const changed = structuredClone(a.intent);
    changed.dependencies[0].desiredRevision = "d".repeat(64);
    const b = await desired(a.identity, changed, a.revision);
    await put(b);
    expect(await (await put(a)).json()).toMatchObject({ revision: b.revision });
    expect(
      await (await put({ ...a, expected_revision: b.revision })).json(),
    ).toMatchObject({ revision: a.revision });
    expect(
      await (await request(resourcePath(a.identity))).json(),
    ).toMatchObject({ revision: a.revision });
  });
  it("rejects altered digests and ownership changes", async () => {
    const d = await desired(
      identity("PiHarness", "owner"),
      harnessIntent("owner"),
    );
    await put(d);
    expect((await put({ ...d, revision: "f".repeat(64) })).status).toBe(422);
    const intent = structuredClone(d.intent);
    intent.userBinding = "another";
    intent.spec.user_id = "another";
    expect(
      (await put(await desired(d.identity, intent, d.revision))).status,
    ).toBe(409);
    const changedIdentity = { ...d.identity, name: "different-name" };
    expect((await put(await desired(changedIdentity, d.intent))).status).toBe(
      409,
    );
  });
  it("isolates new UID state while retaining prior UID", async () => {
    const a = await desired(
      identity("PiHarness", "retain"),
      harnessIntent("retain"),
    );
    await put(a);
    const b = await desired({ ...a.identity, uid: "new-uid-retain" }, a.intent);
    expect((await put(b)).status).toBe(201);
    for (const d of [a, b])
      expect(
        await (await request(resourcePath(d.identity))).json(),
      ).toMatchObject({ ready: true, revision: d.revision });
  });
  it("requires parent UID/revision then opens a real isolated root that survives eviction", async () => {
    const { parent, child } = await seed("root");
    const response = await put(child);
    expect(response.status).toBe(201);
    const first = await response.json<any>();
    expect(first).toMatchObject({
      ready: true,
      phase: "Ready",
      revision: child.revision,
      conversation_id: 1,
    });
    const stub = bindings.RESOURCES.getByName(resourceName(child.identity));
    expect(
      await runInDurableObject(stub, (_instance, state) =>
        state.storage.getAlarm(),
      ),
    ).toBe(null);
    await evictDurableObject(stub);
    const reopened = await (
      await request(resourcePath(child.identity))
    ).json<any>();
    expect(reopened).toMatchObject(first);
    const snapshot = await (
      await request(`${resourcePath(child.identity)}/snapshot`)
    ).json<any>();
    expect(snapshot).toMatchObject({
      conversation_id: 1,
      entries: [],
      active_tasks: 0,
    });
    const changed = structuredClone(parent.intent);
    changed.dependencies[0].desiredRevision = "c".repeat(64);
    await put(await desired(parent.identity, changed, parent.revision));
    expect(
      await (await request(resourcePath(child.identity))).json(),
    ).toMatchObject({ ready: false, phase: "DependencyNotReady" });
    const newChild = await desired(
      child.identity,
      sessionIntent("root", await desired(parent.identity, changed)),
      child.revision,
    );
    expect(await (await put(newChild)).json()).toMatchObject({
      ready: true,
      conversation_id: 1,
    });
  });
  it("does not claim readiness when parent is absent", async () => {
    const parent = await desired(
      identity("PiHarness", "absent-parent"),
      harnessIntent("absent"),
    );
    const child = await desired(
      identity("Session", "absent-child"),
      sessionIntent("absent", parent),
    );
    const response = await put(child);
    expect(response.status).toBe(201);
    expect(await response.json()).toMatchObject({
      exists: true,
      ready: false,
      phase: "DependencyNotReady",
    });
  });
  it("rejects changing a session channel or harness owner", async () => {
    const { child } = await seed("immutable");
    await put(child);
    const intent = structuredClone(child.intent);
    intent.spec.channel.name = "#other";
    intent.sessionContext!.channel = "#other";
    expect(
      (await put(await desired(child.identity, intent, child.revision))).status,
    ).toBe(409);
  });
  it("preserves canonical metadata schemes and all standard channel prefixes", async () => {
    const { parent } = await seed("metadata");
    for (const prefix of ["+", "!"]) {
      const intent = sessionIntent("metadata", parent);
      intent.spec.channel.name = `${prefix}room`;
      intent.sessionContext!.channel = `${prefix}room`;
      intent.spec.share_url = "custom://example.test/shared";
      intent.sessionContext!.shareURL = intent.spec.share_url;
      intent.spec.context_ref = "file:///retained/context";
      intent.sessionContext!.contextRef = intent.spec.context_ref;
      expect(
        (
          await put(
            await desired(
              identity("Session", prefix === "+" ? "plus" : "bang"),
              intent,
            ),
          )
        ).status,
      ).toBe(201);
    }
    for (const ref of [
      "https://example.test/?access_token=secret",
      "custom://user:pass@example.test/",
      "https://user%40example.test/",
    ]) {
      const intent = sessionIntent("metadata", parent);
      intent.spec.share_url = ref;
      intent.sessionContext!.shareURL = ref;
      expect(
        (await put(await desired(identity("Session", "bad-reference"), intent)))
          .status,
      ).toBe(422);
    }
  });
  it("clears an obsolete recovery alarm after a real Session cold wake", async () => {
    const { child } = await seed("alarm");
    await put(child);
    const stub = bindings.RESOURCES.getByName(resourceName(child.identity));
    await runInDurableObject(stub, (_instance, state) =>
      state.storage.setAlarm(Date.now() + 60_000),
    );
    await evictDurableObject(stub);
    expect(await runDurableObjectAlarm(stub)).toBe(true);
    expect(
      await runInDurableObject(stub, (_instance, state) =>
        state.storage.getAlarm(),
      ),
    ).toBe(null);
  });
  it("rejects input request ID reuse with different text before admitting a model turn", async () => {
    const { child } = await seed("input-conflict");
    await put(child);
    const stub = bindings.RESOURCES.getByName(resourceName(child.identity));
    const digest = await sha256(canonical("original content"));
    await runInDurableObject(stub, (_instance, state) =>
      state.storage.put("cuse:input:stable-id", digest),
    );
    expect(
      (
        await request(`${resourcePath(child.identity)}/input`, "POST", {
          request_id: "stable-id",
          content: "changed content",
        })
      ).status,
    ).toBe(409);
  });
  it("requires auth on transcript and input endpoints and refuses arbitrary command shape", async () => {
    const { child } = await seed("private");
    await put(child);
    expect(
      (
        await request(
          `${resourcePath(child.identity)}/snapshot`,
          "GET",
          undefined,
          "",
        )
      ).status,
    ).toBe(401);
    expect(
      (
        await request(
          `${resourcePath(child.identity)}/input`,
          "POST",
          { request_id: "one", content: "x" },
          "",
        )
      ).status,
    ).toBe(401);
    expect(
      (
        await request(`${resourcePath(child.identity)}/input`, "POST", {
          request_id: "one",
          content: "x",
          tools: ["shell"],
        })
      ).status,
    ).toBe(422);
  });
});
describe("Python digest compatibility", () => {
  it("accepts actual canonical Python controller payloads", async () => {
    for (const plan of [pythonPlans.PiHarness, pythonPlans.Session]) {
      const response = await put(plan as Desired);
      expect(response.status).toBe(201);
      expect(await response.json()).toMatchObject({
        ready: true,
        revision: plan.revision,
      });
    }
  });
  it("matches ensure_ascii including Unicode and code-point key order", async () => {
    expect(canonical({ "😀": "é\u007f\u2028", "\ue000": "𐐀" })).toBe(
      '{"\\ue000":"\\ud801\\udc00","\\ud83d\\ude00":"\\u00e9\\u007f\\u2028"}',
    );
    expect(await sha256(canonical({ a: "café", b: ["🤖", "#研究"] }))).toBe(
      "e928e4ba63071fc08eeeec29e245af2e6a2e40dbc2cf8c4b602e8d996ddcd294",
    );
  });
});
