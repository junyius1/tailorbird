// 会话层:本地 token 登录 → tb_session cookie → api.me()。
// 未登录(api.me 401)→ 返回 null,登录门据此跳 /login。
import { queryOptions, useQuery } from "@tanstack/react-query"

import { ApiError, api } from "./api"
import type { SessionUser } from "./api"

export const sessionQueryOptions = queryOptions<SessionUser | null>({
  queryKey: ["session"],
  queryFn: async () => {
    try {
      return await api.me()
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return null
      throw e
    }
  },
  staleTime: 60_000,
  retry: false,
})

export function useSession() {
  return useQuery(sessionQueryOptions)
}

/** 本地登录:username + 共享 token → 写 tb_session cookie。 */
export function login(username: string, token: string) {
  return fetch("/dashboard/api/auth/login", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, token }),
  }).then(async (res) => {
    if (!res.ok) {
      let detail = res.statusText
      try {
        const body = await res.json()
        if (body?.detail) detail = String(body.detail)
      } catch {
        /* ignore */
      }
      throw new ApiError(res.status, detail)
    }
    return res.json()
  })
}

export function logout() {
  return api.logout()
}
