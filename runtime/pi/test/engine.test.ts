import { env } from "cloudflare:workers";
import {
  evictDurableObject,
  runDurableObjectAlarm,
  runInDurableObject,
} from "cloudflare:test";
import { BACKGROUND_CONTEXT as context } from "@earendil-works/chord/context";
import {
  createModels,
  fauxProvider,
  fauxAssistantMessage,
  type FauxResponseStep,
} from "@earendil-works/pi-ai";
import { describe, expect, it, vi } from "vitest";
import { PiEngine } from "../src/engine.ts";
import {
  authenticate,
  modelRuntime,
  type Env,
  type ModelRuntime,
} from "../src/config.ts";
const bindings = env as unknown as Env;
function fakeRuntime(
  responses: FauxResponseStep[],
  fingerprint = "faux-version-one",
): ModelRuntime {
  const models = createModels();
  const faux = fauxProvider();
  faux.setResponses(responses);
  models.setProvider(faux.provider);
  return { models, provider: "faux", modelId: "faux-1", fingerprint };
}
describe("real Pi SQLite task recovery with an offline faux provider", () => {
  it("durably deduplicates a prompt, recovers interrupted work after eviction, and clears idle alarm", async () => {
    const stub = bindings.RESOURCES.getByName("offline-recovery-test");
    const started = Promise.withResolvers<void>();
    const interrupt: FauxResponseStep = async (_request, options) => {
      started.resolve();
      return new Promise((_resolve, reject) => {
        const abort = () =>
          reject(new DOMException("Local test interruption", "AbortError"));
        if (options?.signal?.aborted) abort();
        else options?.signal?.addEventListener("abort", abort, { once: true });
      });
    };
    let first: number | undefined;
    await runInDurableObject(stub, async (_instance, state) => {
      const engine = await PiEngine.open(
        state.storage,
        fakeRuntime([interrupt]),
      );
      try {
        first = (
          await engine.submit("recovery-request", "Complete this offline test")
        ).submission_id;
        expect(
          (
            await engine.submit(
              "recovery-request",
              "Complete this offline test",
            )
          ).submission_id,
        ).toBe(first);
        await started.promise;
        await engine.submit(
          "queued-request",
          "Also finish the queued offline input",
        );
        expect(await state.storage.getAlarm()).not.toBe(null);
      } finally {
        await engine.close();
      }
    });
    await evictDurableObject(stub);
    await runInDurableObject(stub, async (_instance, state) => {
      await expect(
        PiEngine.open(
          state.storage,
          fakeRuntime([], "changed-version"),
          "faux-version-one",
        ),
      ).rejects.toMatchObject({ code: "runtime_change_with_active_tasks" });
      const engine = await PiEngine.open(
        state.storage,
        fakeRuntime([
          fauxAssistantMessage("Recovered offline."),
          fauxAssistantMessage("Queued input recovered too."),
        ]),
        "faux-version-one",
      );
      try {
        await engine.refreshAlarm();
        await engine.root.waitForIdle(context);
        await engine.refreshAlarm();
        expect(
          (
            await engine.submit(
              "recovery-request",
              "Complete this offline test",
            )
          ).submission_id,
        ).toBe(first);
        const snapshot = (await engine.snapshot()) as any;
        expect(snapshot.active_tasks).toBe(0);
        expect(JSON.stringify(snapshot.entries)).toContain(
          "Recovered offline.",
        );
        expect(JSON.stringify(snapshot.entries)).toContain(
          "Queued input recovered too.",
        );
        expect(await state.storage.getAlarm()).toBe(null);
      } finally {
        await engine.close();
      }
    });
    expect(await runDurableObjectAlarm(stub)).toBe(false);
  });
  it("serializes alarm-before-admission against a paused idle-alarm deletion", async () => {
    const stub = bindings.RESOURCES.getByName("offline-alarm-race-test");
    await runInDurableObject(stub, async (_instance, state) => {
      const interrupted: FauxResponseStep = async (_request, options) =>
        new Promise((_resolve, reject) => {
          const abort = () =>
            reject(new DOMException("Local interruption", "AbortError"));
          if (options?.signal?.aborted) abort();
          else
            options?.signal?.addEventListener("abort", abort, { once: true });
        });
      const engine = await PiEngine.open(
        state.storage,
        fakeRuntime([interrupted]),
      );
      const inspected = Promise.withResolvers<void>();
      const release = Promise.withResolvers<void>();
      const originalInspect = engine.harness.inspect.bind(engine.harness);
      const inspectSpy = vi
        .spyOn(engine.harness, "inspect")
        .mockImplementationOnce(async (ctx) => {
          const empty = await originalInspect(ctx);
          inspected.resolve();
          await release.promise;
          return empty;
        });
      const originalSetAlarm = state.storage.setAlarm.bind(state.storage);
      const setAlarmSpy = vi
        .spyOn(state.storage, "setAlarm")
        .mockImplementation((at) => originalSetAlarm(at));
      try {
        const idleRefresh = engine.refreshAlarm();
        await inspected.promise;
        const admitted = engine.submit(
          "race-input",
          "Keep a recovery alarm before admission",
        );
        // No timing assumption: an uncoordinated async submit calls setAlarm
        // synchronously before its first await; the shared line must queue it.
        expect(setAlarmSpy).not.toHaveBeenCalled();
        release.resolve();
        await idleRefresh;
        await admitted;
        expect(await state.storage.getAlarm()).not.toBe(null);
      } finally {
        release.resolve();
        inspectSpy.mockRestore();
        setAlarmSpy.mockRestore();
        await engine.close();
      }
    });
  });
  it("drains an in-progress idle alarm mutation before engine close resolves", async () => {
    const stub = bindings.RESOURCES.getByName("offline-close-race-test");
    await runInDurableObject(stub, async (_instance, state) => {
      const engine = await PiEngine.open(state.storage, fakeRuntime([]));
      const inspected = Promise.withResolvers<void>();
      const release = Promise.withResolvers<void>();
      const original = engine.harness.inspect.bind(engine.harness);
      const spy = vi
        .spyOn(engine.harness, "inspect")
        .mockImplementationOnce(async (ctx) => {
          const snapshot = await original(ctx);
          inspected.resolve();
          await release.promise;
          return snapshot;
        });
      const refresh = engine.refreshAlarm();
      await inspected.promise;
      let closed = false;
      const closing = engine.close().then(() => {
        closed = true;
      });
      await Promise.resolve();
      expect(closed).toBe(false);
      release.resolve();
      await refresh;
      await closing;
      spy.mockRestore();
      expect(closed).toBe(true);
      await expect(
        engine.submit("closed", "Must not admit after close"),
      ).rejects.toMatchObject({ code: "runtime_closed" });
    });
  });
  it("reconfigures an idle persisted root only when runtime fingerprint changes", async () => {
    const stub = bindings.RESOURCES.getByName("offline-reconfiguration-test");
    await runInDurableObject(stub, async (_instance, state) => {
      const first = await PiEngine.open(state.storage, fakeRuntime([]));
      await first.close();
      const second = await PiEngine.open(
        state.storage,
        fakeRuntime([], "v2"),
        "faux-version-one",
      );
      try {
        expect(second.root.id).toBe(1);
        expect(second.fingerprint).toBe("v2");
        expect((await second.root.agent(context)).tools).toEqual([]);
      } finally {
        await second.close();
      }
    });
  });
});
describe("fail-closed deployment configuration", () => {
  it("requires explicit provider, model, and existing secret binding", async () => {
    for (const overrides of [
      { PI_MODEL_PROVIDER: undefined },
      { PI_MODEL_PROVIDER: "faux" },
      { PI_MODEL_ID: undefined },
      { PI_MODEL_ID: "not-in-pinned-provider-catalog" },
      { PI_MODEL_SECRET_BINDING: undefined },
      { PI_MODEL_SECRET_BINDING: "CONTROL_API_TOKEN" },
      { PI_MODEL_SECRET_BINDING: "PI_MODEL_PROVIDER" },
      { PI_MODEL_TEST_KEY: "" },
    ])
      await expect(
        modelRuntime({ ...bindings, ...overrides }),
      ).rejects.toThrow();
  });
  it("accepts matching printable control tokens at both length boundaries", async () => {
    for (const length of [32, 8192]) {
      const token = "x".repeat(length);
      await expect(
        authenticate(
          new Request("https://local.test", {
            headers: { Authorization: `Bearer ${token}` },
          }),
          { ...bindings, CONTROL_API_TOKEN: token },
        ),
      ).resolves.toBeUndefined();
    }
  });
  it("fingerprints configuration without deriving public data from credential values", async () => {
    const a = await modelRuntime(bindings);
    const b = await modelRuntime({
      ...bindings,
      PI_MODEL_TEST_KEY: "rotated-local-placeholder",
    });
    expect(a.fingerprint).toBe(b.fingerprint);
    expect(a.models.getModel(a.provider, a.modelId)).toBeDefined();
  });
  it("does not accept requests when control secret is absent", async () => {
    for (const token of [
      undefined,
      "x".repeat(31),
      "x".repeat(8193),
      "é".repeat(32),
    ])
      await expect(
        authenticate(new Request("https://local.test"), {
          ...bindings,
          CONTROL_API_TOKEN: token,
        }),
      ).rejects.toMatchObject({ status: 503 });
  });
});
