import { createModels, type Models } from "@earendil-works/pi-ai/models";
import { openaiProvider } from "@earendil-works/pi-ai/providers/openai";
import { canonical, fail, sha256 } from "./contract.ts";
import type { PiResource } from "./resource.ts";

export interface Env {
  RESOURCES: DurableObjectNamespace<PiResource>;
  CONTROL_API_TOKEN?: string;
  PI_MODEL_PROVIDER?: string;
  PI_MODEL_ID?: string;
  PI_MODEL_SECRET_BINDING?: string;
  [key: string]: unknown;
}
export interface ModelRuntime {
  provider: string;
  modelId: string;
  fingerprint: string;
  models: Models;
}
export async function modelRuntime(env: Env): Promise<ModelRuntime> {
  const provider = env.PI_MODEL_PROVIDER;
  const modelId = env.PI_MODEL_ID;
  const binding = env.PI_MODEL_SECRET_BINDING;
  if (
    provider !== "openai" ||
    !modelId ||
    !binding ||
    !/^PI_MODEL_[A-Z][A-Z0-9_]*$/.test(binding) ||
    ["PI_MODEL_PROVIDER", "PI_MODEL_ID", "PI_MODEL_SECRET_BINDING"].includes(
      binding,
    )
  )
    fail(503, "model_configuration_missing");
  const secret = env[binding];
  if (typeof secret !== "string" || !secret.trim())
    fail(503, "model_secret_missing");
  // No process-wide ambient credentials, OAuth, file reads or catalog network I/O.
  const models = createModels({
    authContext: {
      env: async (name) => (name === "OPENAI_API_KEY" ? secret : undefined),
      fileExists: async () => false,
    },
  });
  models.setProvider(openaiProvider());
  if (!models.getModel(provider, modelId)) fail(422, "unsupported_model");
  const fingerprint = await sha256(
    canonical({
      provider,
      modelId,
      secretBinding: binding,
      runtime: "cuse-pi-v1-tools-disabled",
    }),
  );
  return { provider, modelId, fingerprint, models };
}
export async function authenticate(request: Request, env: Env): Promise<void> {
  const expected = env.CONTROL_API_TOKEN;
  if (
    typeof expected !== "string" ||
    expected.length < 32 ||
    expected.length > 8192 ||
    !/^[!-~]+$/.test(expected)
  )
    fail(503, "control_auth_unconfigured");
  const supplied = request.headers.get("Authorization");
  if (!supplied || !supplied.startsWith("Bearer ") || supplied.length > 8199)
    fail(401, "unauthorized");
  const [a, b] = await Promise.all([
    sha256(expected),
    sha256(supplied.slice(7)),
  ]);
  let different = 0;
  for (let i = 0; i < a.length; i++)
    different |= a.charCodeAt(i) ^ b.charCodeAt(i);
  if (different !== 0) fail(401, "unauthorized");
}
