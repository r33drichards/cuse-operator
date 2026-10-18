import { BACKGROUND_CONTEXT as context } from "@earendil-works/chord/context";
import { DurableObject } from "cloudflare:workers";
import { authenticate, modelRuntime, type Env } from "./config.ts";
import {
  assertOwnership,
  canonical,
  fail,
  isObject,
  parseDesired,
  readJSON,
  resourceName,
  resourcePath,
  route,
  sameIdentity,
  sha256,
  type Identity,
  type Stored,
} from "./contract.ts";
import { PiEngine } from "./engine.ts";
import { errorResponse, json } from "./index.ts";

const RECORD = "cuse:desired:v1";
export class PiResource extends DurableObject<Env> {
  #record?: Stored;
  #engine?: PiEngine;
  #line: Promise<unknown> = Promise.resolve();
  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    void ctx.blockConcurrencyWhile(async () => {
      this.#record = await ctx.storage.get<Stored>(RECORD);
    });
  }
  #serialize<T>(operation: () => Promise<T>): Promise<T> {
    const run = this.#line.then(operation, operation);
    this.#line = run.catch(() => undefined);
    return run;
  }
  override async fetch(request: Request): Promise<Response> {
    return this.#serialize(async () => {
      try {
        // Defense in depth: a same-account Worker binding cannot bypass auth.
        await authenticate(request, this.env);
        const match = route(new URL(request.url).pathname);
        if (!match) return json({ error: "not_found" }, 404);
        if (
          this.#record &&
          !sameIdentity(this.#record.identity, match.identity)
        )
          fail(409, "ownership_conflict");
        if (!match.action && request.method === "PUT")
          return await this.#put(request, match.identity);
        if (!this.#record)
          return json(
            { exists: false, revision: "", ready: false, phase: "Absent" },
            404,
          );
        if (!match.action && request.method === "GET")
          return json(await this.#observation());
        if (this.#record.identity.kind !== "Session")
          return json({ error: "not_found" }, 404);
        if (match.action === "snapshot" && request.method === "GET") {
          const observation = await this.#observation();
          if (!observation.ready || !this.#engine)
            fail(503, "session_not_ready");
          return json(await this.#engine.snapshot());
        }
        if (match.action === "input" && request.method === "POST") {
          const value = await readJSON(request, 20_000);
          if (
            !isObject(value) ||
            Object.keys(value).sort().join(",") !== "content,request_id" ||
            typeof value.request_id !== "string" ||
            !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(value.request_id) ||
            typeof value.content !== "string" ||
            !value.content.trim() ||
            value.content.length > 16_000
          )
            fail(422, "invalid_input");
          const observation = await this.#observation();
          if (!observation.ready || !this.#engine)
            fail(503, "session_not_ready");
          const receiptKey = `cuse:input:${value.request_id}`;
          const digest = await sha256(canonical(value.content));
          const previous = await this.ctx.storage.get<string>(receiptKey);
          if (previous && previous !== digest)
            fail(409, "input_idempotency_conflict");
          if (!previous) await this.ctx.storage.put(receiptKey, digest);
          return json(
            await this.#engine.submit(value.request_id, value.content),
            202,
          );
        }
        return json({ error: "method_not_allowed" }, 405);
      } catch (error) {
        return errorResponse(error);
      } finally {
        if (request.body && !request.bodyUsed && !request.body.locked)
          await request.body.cancel().catch(() => undefined);
      }
    });
  }
  async #put(request: Request, identity: Identity): Promise<Response> {
    const desired = await parseDesired(await readJSON(request), identity);
    const current = this.#record;
    if (current) assertOwnership(current, desired);
    const receiptKey = `cuse:revision:${desired.idempotency_key}`;
    const receipt = await this.ctx.storage.get<string>(receiptKey);
    if (receipt) {
      if (receipt !== desired.revision) fail(409, "idempotency_conflict");
      if (
        desired.revision === current?.revision ||
        desired.expected_revision !== current?.revision
      )
        return json(await this.#observation());
      // Deterministic controller keys repeat when an operator explicitly
      // reverts intent A -> B -> A. Exact current CAS permits that update.
    }
    if (desired.expected_revision !== (current?.revision ?? null))
      fail(409, "revision_conflict");
    // Model selection is deployment configuration, never an operator-provided
    // credential or fallback model. Refuse bad settings before persisting intent.
    await modelRuntime(this.env);
    const next: Stored = {
      identity: desired.identity,
      intent: desired.intent,
      revision: desired.revision,
      idempotency_key: desired.idempotency_key,
      ...(current?.runtime_revision
        ? { runtime_revision: current.runtime_revision }
        : {}),
    };
    await this.ctx.storage.transaction(async (tx) => {
      await tx.put(RECORD, next);
      await tx.put(receiptKey, desired.revision);
    });
    this.#record = next;
    return json(await this.#observation(), current ? 200 : 201);
  }
  async #observation(): Promise<{
    exists: boolean;
    revision: string;
    ready: boolean;
    phase: string;
    runtime_revision?: string;
    conversation_id?: number;
  }> {
    const record = this.#record;
    if (!record)
      return { exists: false, revision: "", ready: false, phase: "Absent" };
    const base = { exists: true, revision: record.revision, ready: false };
    try {
      const runtime = await modelRuntime(this.env);
      if (record.identity.kind === "PiHarness")
        return {
          ...base,
          ready: true,
          phase: "Configured",
          runtime_revision: runtime.fingerprint,
        };
      const parent = record.intent.dependencies[0];
      const response = await this.env.RESOURCES.getByName(
        resourceName(parent),
      ).fetch(`https://control.internal${resourcePath(parent)}`, {
        headers: { Authorization: `Bearer ${this.env.CONTROL_API_TOKEN}` },
      });
      if (!response.ok)
        return {
          ...base,
          phase: "DependencyNotReady",
          runtime_revision: runtime.fingerprint,
        };
      const observed = await response.json<{
        exists: boolean;
        revision: string;
        ready: boolean;
        runtime_revision?: string;
      }>();
      if (
        !observed.exists ||
        !observed.ready ||
        observed.revision !== parent.desiredRevision ||
        observed.runtime_revision !== runtime.fingerprint
      )
        return {
          ...base,
          phase: "DependencyNotReady",
          runtime_revision: runtime.fingerprint,
        };
      if (this.#engine && this.#engine.fingerprint !== runtime.fingerprint) {
        await this.#engine.close();
        this.#engine = undefined;
      }
      if (!this.#engine) {
        const engine = await PiEngine.open(
          this.ctx.storage,
          runtime,
          record.runtime_revision,
        );
        this.#engine = engine;
        void engine.harness.closed.then((end) => {
          if (end.reason === "failed") this.ctx.abort("Durable runtime failed");
        });
      }
      if (record.runtime_revision !== runtime.fingerprint) {
        this.#record = { ...record, runtime_revision: runtime.fingerprint };
        await this.ctx.storage.put(RECORD, this.#record);
      }
      await this.#engine.refreshAlarm();
      if (
        (await this.#engine.harness.inspect(context)).tasks.some(
          (task) => task.state.kind === "blocked",
        )
      )
        return {
          ...base,
          phase: "RuntimeBlocked",
          runtime_revision: runtime.fingerprint,
        };
      return {
        ...base,
        ready: true,
        phase: "Ready",
        runtime_revision: runtime.fingerprint,
        conversation_id: this.#engine.root.id,
      };
    } catch {
      return { ...base, phase: "RuntimeBlocked" };
    }
  }
  override async alarm(): Promise<void> {
    await this.#serialize(async () => {
      const observation = await this.#observation();
      // Pending work must retain a recovery wake even when a dependency/config
      // temporarily blocks opening. No alarm is created for a never-started room.
      if (!observation.ready && this.#record?.identity.kind === "Session")
        await this.ctx.storage.setAlarm(Date.now() + 60_000);
    });
  }
}
