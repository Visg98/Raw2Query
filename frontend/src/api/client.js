// Thin fetch wrapper: base URL, JSON handling, normalized errors.

export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";

/**
 * Shared token for the public demo deployment, checked by Caddy in front of the
 * API (see docker/Caddyfile). Empty in local development, where nothing checks
 * it, so the header is simply omitted.
 *
 * This is not authentication — it is inlined into the built bundle by Vite and
 * therefore public. It exists to keep crawlers and drive-by scanners from
 * spending the deployment's LLM rate limit.
 *
 * Note that `fileUrl()` below and the EventSource in hooks/useJobEvents.js
 * cannot carry this header at all, which is why the Caddy gate exempts GET on
 * /documents/* and /jobs/*.
 */
const DEMO_TOKEN = import.meta.env.VITE_DEMO_TOKEN || "";

/**
 * FastAPI's error body is `{"detail": ...}`, but `detail` itself is a
 * string for a hand-raised HTTPException and a *list* of
 * `{loc, msg, type}` objects for a 422 request-validation failure. Passing
 * that list straight into `Error(message)` stringifies it as
 * "[object Object]" (Array.prototype.toString on a list of objects) —
 * readable nowhere. Render both shapes as text instead.
 */
function formatErrorDetail(detail) {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (typeof item === "string") return item;
        const field = Array.isArray(item?.loc) ? item.loc.filter((p) => p !== "body").join(".") : null;
        return field ? `${field}: ${item?.msg ?? item}` : item?.msg || JSON.stringify(item);
      })
      .join("; ");
  }
  if (detail && typeof detail === "object") return detail.msg || JSON.stringify(detail);
  return null;
}

export class ApiError extends Error {
  constructor(status, body) {
    super(
      (typeof body === "string" ? body : formatErrorDetail(body?.detail)) || `Request failed (${status})`,
    );
    this.status = status;
    this.body = body;
  }
}

async function parseBody(res) {
  const text = await res.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

/**
 * @param {string} path - e.g. "/schemas"
 * @param {object} [options]
 * @param {string} [options.method]
 * @param {object} [options.json] - body to JSON-encode
 * @param {FormData} [options.form] - multipart body (mutually exclusive with json)
 * @param {object} [options.params] - query params (arrays repeated)
 */
export async function apiFetch(path, options = {}) {
  const { method = "GET", json, form, params, signal } = options;
  const url = new URL(path, API_BASE_URL);
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      if (value === undefined || value === null || value === "") continue;
      if (Array.isArray(value)) {
        value.forEach((v) => url.searchParams.append(key, v));
      } else {
        url.searchParams.set(key, value);
      }
    }
  }

  const init = { method, signal };
  const headers = DEMO_TOKEN ? { "X-Demo-Token": DEMO_TOKEN } : {};
  if (form) {
    // No Content-Type: the browser has to set it itself so it can add the
    // multipart boundary.
    init.body = form;
  } else if (json !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(json);
  }
  if (Object.keys(headers).length > 0) init.headers = headers;

  const res = await fetch(url, init);

  if (!res.ok) {
    const body = await parseBody(res);
    throw new ApiError(res.status, body);
  }

  if (res.status === 204) return null;

  const contentType = res.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    return parseBody(res);
  }
  return res;
}

export function fileUrl(path) {
  return new URL(path, API_BASE_URL).toString();
}
