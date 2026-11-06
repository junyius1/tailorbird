// /dashboard/api 的最小 HTTP 封装(浏览器同 origin;osw_session cookie 自动携带)。
// 约定:非 2xx 抛 ApiError;UI 不手填 Authorization,身份全部来自会话 cookie。

// API 错误:带 status,便于 UI 区分 401(未登录)/404(不属主)/其它。
export class ApiError extends Error {
  // HTTP 状态码
  status: number
  // 构造:消息 + 状态码
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

// 基础 fetch:统一错误处理
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, { credentials: "same-origin", ...init })
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = (await res.json()) as { detail?: unknown }
      if (body.detail) detail = JSON.stringify(body.detail)
    } catch {
      // 非 JSON 错误体:用 statusText
    }
    throw new ApiError(res.status, detail)
  }
  return (await res.json()) as T
}

// GET JSON
export function getJson<T>(path: string): Promise<T> {
  return request<T>(path)
}

// POST JSON
export function postJson<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
}

// PUT JSON
export function putJson<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  })
}

// DELETE(204 → undefined)
export async function deleteJson(path: string): Promise<void> {
  const res = await fetch(path, { credentials: "same-origin", method: "DELETE" })
  if (!res.ok && res.status !== 204) throw new ApiError(res.status, res.statusText)
}

// SSE:连 /dashboard/api/threads/{id}/stream/events(v2 事件流透传)。
// 返回 EventSource(调用方负责 close);onmessage 由调用方挂。
export function openStream(threadId: string): EventSource {
  return new EventSource(`/dashboard/api/threads/${threadId}/stream/events`)
}
