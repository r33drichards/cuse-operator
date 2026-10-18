import { authenticate, type Env } from "./config.ts";
import { ProtocolError, readJSON, resourceName, route } from "./contract.ts";
export { PiResource } from "./resource.ts";
export const json = (
  body: unknown,
  status = 200,
  headers: HeadersInit = {},
): Response =>
  Response.json(body, {
    status,
    headers: {
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff",
      ...headers,
    },
  });
export function errorResponse(error: unknown): Response {
  if (error instanceof ProtocolError)
    return json({ error: error.code }, error.status);
  return json({ error: "runtime_unavailable" }, 503);
}
export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    try {
      await authenticate(request, env);
      const url = new URL(request.url);
      if (url.search || url.hash)
        return json({ error: "query_not_supported" }, 400);
      const match = route(url.pathname);
      if (!match) return json({ error: "not_found" }, 404);
      const allowed =
        match.action === "input"
          ? ["POST"]
          : match.action === "snapshot"
            ? ["GET"]
            : ["GET", "PUT"];
      if (!allowed.includes(request.method))
        return json({ error: "method_not_allowed" }, 405, {
          Allow: allowed.join(", "),
        });
      if (match.action && match.identity.kind !== "Session")
        return json({ error: "not_found" }, 404);
      const forwarded = ["PUT", "POST"].includes(request.method)
        ? new Request(request, {
            body: JSON.stringify(
              await readJSON(
                request,
                match.action === "input" ? 20_000 : 65_536,
              ),
            ),
          })
        : request;
      return await env.RESOURCES.getByName(resourceName(match.identity)).fetch(
        forwarded,
      );
    } catch (error) {
      return errorResponse(error);
    } finally {
      if (request.body && !request.bodyUsed && !request.body.locked)
        await request.body.cancel().catch(() => undefined);
    }
  },
} satisfies ExportedHandler<Env>;
