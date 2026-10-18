import { cloudflareTest } from "@cloudflare/vitest-plugin";
import { defineConfig } from "vitest/config";
export default defineConfig({
  test: { fsModuleCache: false },
  plugins: [
    cloudflareTest({
      wrangler: { configPath: "./wrangler.jsonc" },
      miniflare: {
        outboundService: async (request) => {
          await request.arrayBuffer();
          return new Response("External network disabled in local tests", {
            status: 502,
          });
        },
        bindings: {
          CONTROL_API_TOKEN: "local-control-test-token-never-production",
          PI_MODEL_PROVIDER: "openai",
          PI_MODEL_ID: "gpt-4.1-mini",
          PI_MODEL_SECRET_BINDING: "PI_MODEL_TEST_KEY",
          PI_MODEL_TEST_KEY: "local-placeholder-not-a-real-key",
        },
      },
    }),
  ],
});
