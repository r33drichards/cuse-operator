import { BACKGROUND_CONTEXT as context } from "@earendil-works/chord/context";
import {
  Harness,
  createRegistry,
  type Conversation,
  type TaskId,
} from "@earendil-works/pi-durable";
import { openDurableObjectSqliteStorage } from "@earendil-works/pi-durable/storage/sqlite/cloudflare";
import type { ModelRuntime } from "./config.ts";
import { fail } from "./contract.ts";

export const RECOVERY_DELAY_MS = 60_000;
/** One Pi Harness and exactly its reserved root conversation per canonical Session. */
export class PiEngine {
  readonly harness: Harness;
  readonly root: Conversation;
  readonly fingerprint: string;
  readonly #storage: DurableObjectStorage;
  readonly #watched = new Set<TaskId>();
  #closed = false;
  #alarmLine: Promise<unknown> = Promise.resolve();
  private constructor(
    harness: Harness,
    root: Conversation,
    storage: DurableObjectStorage,
    fingerprint: string,
  ) {
    this.harness = harness;
    this.root = root;
    this.#storage = storage;
    this.fingerprint = fingerprint;
  }
  static async open(
    storage: DurableObjectStorage,
    runtime: ModelRuntime,
    previousFingerprint?: string,
  ): Promise<PiEngine> {
    const harness = await Harness.open(
      await openDurableObjectSqliteStorage(storage),
      {
        models: runtime.models,
        registry: createRegistry(),
        settings: { documentRetentionMs: 0 },
        onReport: () => {
          /* Do not log provider errors, prompts or credentials. */
        },
      },
      context,
    );
    try {
      const inspection = await harness.inspect(context);
      if (
        previousFingerprint &&
        previousFingerprint !== runtime.fingerprint &&
        inspection.tasks.length > 0
      )
        fail(409, "runtime_change_with_active_tasks");
      const root = await harness.root(context, {
        agent: {
          model: { provider: runtime.provider, modelId: runtime.modelId },
          thinkingLevel: "off",
          tools: [],
        },
      });
      if (previousFingerprint !== runtime.fingerprint) {
        await root.configure(
          {
            model: { provider: runtime.provider, modelId: runtime.modelId },
            thinkingLevel: "off",
            tools: [],
          },
          context,
        );
      }
      const agent = await root.agent(context);
      if (
        agent.model?.provider !== runtime.provider ||
        agent.model?.modelId !== runtime.modelId ||
        agent.tools.length !== 0
      )
        fail(409, "runtime_configuration_drift");
      return new PiEngine(harness, root, storage, runtime.fingerprint);
    } catch (error) {
      await harness.close(context);
      throw error;
    }
  }
  async submit(
    requestId: string,
    content: string,
  ): Promise<{ submission_id: number; conversation_id: number }> {
    // The durable platform alarm precedes admission, so eviction between the Pi
    // commit and the HTTP acknowledgement cannot strand an admitted prompt.
    const submission = await this.#serializeAlarm(async () => {
      if (this.#closed) fail(503, "runtime_closed");
      await this.#storage.setAlarm(Date.now() + RECOVERY_DELAY_MS);
      return await this.root.submit(
        { type: "input", content, requestId, whenBusy: "followUp" },
        context,
      );
    });
    void submission
      .wait(context)
      .then(
        () => this.refreshAlarm(),
        () => this.refreshAlarm(),
      )
      .catch(() => undefined);
    await this.refreshAlarm();
    return { submission_id: submission.id, conversation_id: this.root.id };
  }
  async snapshot(): Promise<unknown> {
    const page = await this.root.entries(
      { order: "descending" },
      100,
      undefined,
      context,
    );
    const { tasks, submissions } = await this.harness.inspect(context);
    return {
      conversation_id: this.root.id,
      entries: page.items,
      next_cursor: page.next ?? null,
      pending_inputs: submissions.map((s) => ({
        id: s.id,
        request_id: s.requestId,
        status: s.status,
      })),
      active_tasks: tasks.length,
      blocked_tasks: tasks.filter((t) => t.state.kind === "blocked").length,
    };
  }
  async refreshAlarm(): Promise<void> {
    const run = async () => {
      if (this.#closed) return;
      const { tasks } = await this.harness.inspect(context);
      const actionable = tasks.filter((t) => t.state.kind !== "blocked");
      if (actionable.length === 0) {
        await this.#storage.deleteAlarm();
        return;
      }
      await this.#storage.setAlarm(Date.now() + RECOVERY_DELAY_MS);
      // Reading a session never blindly resumes before an alarm is durable.
      for (const task of actionable) {
        if (this.#watched.has(task.record.id)) continue;
        this.#watched.add(task.record.id);
        void this.harness
          .waitForTask(task.record.id, context)
          .then(
            () => this.refreshAlarm(),
            () => undefined,
          )
          .catch(() => undefined)
          .finally(() => this.#watched.delete(task.record.id));
      }
    };
    await this.#serializeAlarm(run);
  }
  #serializeAlarm<T>(operation: () => Promise<T>): Promise<T> {
    const next = this.#alarmLine.then(operation, operation);
    this.#alarmLine = next.then(
      () => undefined,
      () => undefined,
    );
    return next;
  }
  async close(): Promise<void> {
    this.#closed = true;
    // Join any already-entered alarm mutation before a replacement engine
    // can admit work. Later queued refresh callbacks see #closed and do nothing.
    await this.#alarmLine;
    await this.harness.close(context);
  }
}
