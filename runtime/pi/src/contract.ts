/** Controller protocol, deliberately independent of Pi and Cloudflare. */
export type Kind = "PiHarness" | "Session";
export interface Identity {
  kind: Kind;
  namespace: string;
  name: string;
  uid: string;
}
export interface Dependency extends Identity {
  kind: any;
  specDigest: string;
  desiredRevision: string;
}
export interface Intent {
  kind: Kind;
  spec: Record<string, any>;
  dependencies: Dependency[];
  userBinding?: string;
  sessionContext?: { channel: string; contextRef: string; shareURL: string };
}
export interface Desired {
  identity: Identity;
  intent: Intent;
  revision: string;
  idempotency_key: string;
  expected_revision: string | null;
}
export interface Stored extends Omit<Desired, "expected_revision"> {
  runtime_revision?: string;
}
export class ProtocolError extends Error {
  readonly status: number;
  readonly code: string;
  constructor(status: number, code: string) {
    super(code);
    this.status = status;
    this.code = code;
  }
}
export function fail(status: number, code: string): never {
  throw new ProtocolError(status, code);
}
export const isObject = (v: unknown): v is Record<string, any> =>
  typeof v === "object" && v !== null && !Array.isArray(v);
const exact = (
  v: unknown,
  keys: string[],
  optional: string[] = [],
): v is Record<string, any> =>
  isObject(v) &&
  keys.every((k) => Object.hasOwn(v, k)) &&
  Object.keys(v).every((k) => keys.includes(k) || optional.includes(k));
const scalar = (v: unknown, max = 2048): v is string =>
  typeof v === "string" &&
  v.length > 0 &&
  v.length <= max &&
  v.trim() === v &&
  !/[\x00-\x1f\x7f]/u.test(v);
export const digestPattern = /^[0-9a-f]{64}$/;
const digest = (v: unknown): v is string =>
  typeof v === "string" && digestPattern.test(v);
const dnsName = (v: unknown): v is string =>
  scalar(v, 253) &&
  /^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$/.test(v);
const reference = (v: unknown): boolean =>
  exact(v, ["name"], ["uid"]) &&
  dnsName(v.name) &&
  (v.uid === undefined || scalar(v.uid, 253));
export function validIdentity(v: unknown): v is Identity {
  return (
    exact(v, ["kind", "namespace", "name", "uid"]) &&
    ["PiHarness", "Session"].includes(v.kind) &&
    dnsName(v.namespace) &&
    dnsName(v.name) &&
    scalar(v.uid, 253) &&
    !/[\/\\]/.test(v.uid)
  );
}
export function sameIdentity(a: Identity, b: Identity): boolean {
  return (
    a.kind === b.kind &&
    a.namespace === b.namespace &&
    a.name === b.name &&
    a.uid === b.uid
  );
}
export function resourceName(identity: Identity): string {
  return `${identity.kind}:${identity.uid}`;
}
export function resourcePath(identity: Identity): string {
  return `/v1/resources/${[identity.kind, identity.namespace, identity.name, identity.uid].map(encodeURIComponent).join("/")}`;
}
export function route(
  pathname: string,
): { identity: Identity; action?: "input" | "snapshot" } | undefined {
  const match =
    /^\/v1\/resources\/(PiHarness|Session)\/([^/]+)\/([^/]+)\/([^/]+)(?:\/(input|snapshot))?$/.exec(
      pathname,
    );
  if (!match) return;
  let identity: Identity;
  try {
    identity = {
      kind: match[1] as Kind,
      namespace: decodeURIComponent(match[2]),
      name: decodeURIComponent(match[3]),
      uid: decodeURIComponent(match[4]),
    };
  } catch {
    return fail(400, "invalid_path");
  }
  if (!validIdentity(identity)) fail(400, "invalid_path");
  return { identity, action: match[5] as "input" | "snapshot" | undefined };
}

// Python json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=True).
// The supported intent is only objects, lists and strings, so JS/Python number
// rendering differences cannot change a controller revision.
export function canonical(value: unknown): string {
  if (typeof value === "string")
    return JSON.stringify(value).replace(
      /[\u007f-\uffff]/g,
      (c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, "0")}`,
    );
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (isObject(value))
    return `{${Object.keys(value)
      .sort((a, b) => {
        const x = Array.from(a, (c) => c.codePointAt(0)!);
        const y = Array.from(b, (c) => c.codePointAt(0)!);
        for (let i = 0; i < Math.min(x.length, y.length); i++)
          if (x[i] !== y[i]) return x[i] - y[i];
        return x.length - y.length;
      })
      .map((k) => `${canonical(k)}:${canonical(value[k])}`)
      .join(",")}}`;
  return fail(422, "invalid_intent");
}
export async function sha256(text: string): Promise<string> {
  const bytes = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(text),
  );
  return Array.from(new Uint8Array(bytes), (n) =>
    n.toString(16).padStart(2, "0"),
  ).join("");
}
const credentialKeys = new Set([
  "password",
  "passwd",
  "pwd",
  "secret",
  "token",
  "accesstoken",
  "refreshtoken",
  "apikey",
  "authorization",
  "credential",
  "credentials",
  "clientsecret",
  "signature",
  "sig",
  "xamzsignature",
  "xamzcredential",
  "xgoogsignature",
  "xgoogcredential",
]);
function checkMetadataReference(value: string, storage: boolean): void {
  if (/[\s\u0085\\]/u.test(value)) fail(422, "invalid_reference");
  const authority = /^(?:[a-zA-Z][a-zA-Z0-9+.-]*:)?\/\/([^/?#]*)/.exec(
    value,
  )?.[1];
  try {
    if (
      authority !== undefined &&
      (authority.includes("@") || decodeURIComponent(authority).includes("@"))
    )
      fail(422, "invalid_reference");
    if (
      !storage &&
      (!/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(value) || !authority)
    )
      fail(422, "invalid_reference");
    const url = new URL(
      value,
      storage ? "https://metadata.invalid/" : undefined,
    );
    if (url.username || url.password || (!storage && !url.hostname))
      fail(422, "invalid_reference");
    for (const parameters of [url.search.slice(1), url.hash.slice(1)]) {
      for (const [key] of new URLSearchParams(parameters))
        if (credentialKeys.has(key.toLowerCase().replace(/[^a-z0-9]/g, "")))
          fail(422, "invalid_reference");
    }
  } catch {
    fail(422, "invalid_reference");
  }
}
function checkIntent(
  intent: unknown,
  identity: Identity,
): asserts intent is Intent {
  if (
    !isObject(intent) ||
    intent.kind !== identity.kind ||
    !Array.isArray(intent.dependencies)
  )
    fail(422, "invalid_intent");
  const dependencies = intent.dependencies;
  const expectedKinds =
    identity.kind === "PiHarness"
      ? ["WeeChat"]
      : ["PiHarness", "IRCServer", "MCPClient"];
  if (dependencies.length !== expectedKinds.length)
    fail(422, "invalid_dependencies");
  for (let i = 0; i < expectedKinds.length; i++) {
    const d = dependencies[i];
    if (
      !exact(d, [
        "kind",
        "namespace",
        "name",
        "uid",
        "specDigest",
        "desiredRevision",
      ]) ||
      d.kind !== expectedKinds[i] ||
      d.namespace !== identity.namespace ||
      !dnsName(d.name) ||
      !scalar(d.uid, 253) ||
      !digest(d.specDigest) ||
      !digest(d.desiredRevision)
    )
      fail(422, "invalid_dependencies");
  }
  const spec = intent.spec;
  if (identity.kind === "PiHarness") {
    if (
      !exact(intent, ["kind", "spec", "dependencies", "userBinding"]) ||
      !exact(spec, ["id", "user_id", "weechatRef"]) ||
      !scalar(spec.id) ||
      !scalar(spec.user_id) ||
      !reference(spec.weechatRef) ||
      intent.userBinding !== spec.user_id ||
      dependencies[0].name !== spec.weechatRef.name ||
      (spec.weechatRef.uid !== undefined &&
        spec.weechatRef.uid !== dependencies[0].uid)
    )
      fail(422, "invalid_intent");
  } else {
    if (
      !exact(intent, ["kind", "spec", "dependencies", "sessionContext"]) ||
      !exact(spec, [
        "id",
        "harnessRef",
        "channel",
        "context_ref",
        "share_url",
        "mcpClientRef",
      ]) ||
      !scalar(spec.id) ||
      !scalar(spec.context_ref) ||
      !scalar(spec.share_url) ||
      !reference(spec.harnessRef) ||
      !reference(spec.mcpClientRef) ||
      !exact(spec.channel, ["serverRef", "name"]) ||
      !reference(spec.channel.serverRef) ||
      !scalar(spec.channel.name) ||
      !/^[#&+!][^\s\u0085,:\x07]+$/.test(spec.channel.name)
    )
      fail(422, "invalid_intent");
    const refs = [spec.harnessRef, spec.channel.serverRef, spec.mcpClientRef];
    if (
      refs.some(
        (ref, i) =>
          ref.name !== dependencies[i].name ||
          (ref.uid !== undefined && ref.uid !== dependencies[i].uid),
      )
    )
      fail(422, "invalid_dependencies");
    if (
      !exact(intent.sessionContext, ["channel", "contextRef", "shareURL"]) ||
      intent.sessionContext.channel !== spec.channel.name ||
      intent.sessionContext.contextRef !== spec.context_ref ||
      intent.sessionContext.shareURL !== spec.share_url
    )
      fail(422, "invalid_intent");
    checkMetadataReference(spec.share_url, false);
    checkMetadataReference(spec.context_ref, true);
  }
}
export async function parseDesired(
  value: unknown,
  identity: Identity,
): Promise<Desired> {
  if (
    !exact(value, [
      "identity",
      "intent",
      "revision",
      "idempotency_key",
      "expected_revision",
    ]) ||
    !validIdentity(value.identity) ||
    !sameIdentity(identity, value.identity) ||
    !digest(value.revision) ||
    !digest(value.idempotency_key) ||
    (value.expected_revision !== null && !digest(value.expected_revision))
  )
    fail(422, "invalid_request");
  checkIntent(value.intent, identity);
  if (
    value.revision !== (await sha256(canonical(value.intent))) ||
    value.idempotency_key !==
      (await sha256(
        canonical([
          identity.kind,
          identity.namespace,
          identity.name,
          identity.uid,
          value.revision,
        ]),
      ))
  )
    fail(422, "invalid_digest");
  return value as Desired;
}
export function assertOwnership(current: Stored, desired: Desired): void {
  if (
    !sameIdentity(current.identity, desired.identity) ||
    current.intent.spec.id !== desired.intent.spec.id
  )
    fail(409, "ownership_conflict");
  if (
    current.identity.kind === "PiHarness" &&
    current.intent.userBinding !== desired.intent.userBinding
  )
    fail(409, "ownership_conflict");
  if (current.identity.kind === "Session") {
    const before = current.intent;
    const after = desired.intent;
    if (
      before.dependencies[0].uid !== after.dependencies[0].uid ||
      before.dependencies[1].uid !== after.dependencies[1].uid ||
      before.spec.channel.name !== after.spec.channel.name
    )
      fail(409, "ownership_conflict");
  }
}
export async function readJSON(
  request: Request,
  limit = 65_536,
): Promise<unknown> {
  if (
    !request.headers
      .get("Content-Type")
      ?.split(";")[0]
      .trim()
      .match(/^application\/json$/i)
  )
    fail(415, "json_required");
  if (Number(request.headers.get("Content-Length")) > limit)
    fail(413, "body_too_large");
  if (!request.body) fail(400, "invalid_json");
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let length = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      length += value.byteLength;
      if (length > limit) fail(413, "body_too_large");
      chunks.push(value);
    }
  } finally {
    await reader.cancel();
  }
  const bytes = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.length;
  }
  try {
    return JSON.parse(
      new TextDecoder("utf-8", { fatal: true, ignoreBOM: false }).decode(bytes),
    );
  } catch {
    return fail(400, "invalid_json");
  }
}
