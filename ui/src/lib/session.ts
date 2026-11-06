// 会话层:本地 token 登录(osw_session cookie)/ 登出 / 当前用户。
// 浏览器同 origin 访问 /dashboard/api,身份全部来自 cookie,不手填 Authorization。
import { useQuery } from "@tanstack/react-query"

import { getJson, postJson } from "./api"

// 当前用户(与 dashboard /me 对齐)
export interface CurrentUser {
  // 登录名(= sub)
  sub: string
  // 展示名
  name: string | null
  // 邮箱(本地变体可空)
  email: string | null
  // 是否 admin
  is_admin: boolean
}

// 登录:username + 共享 token → 写 osw_session cookie
export function login(username: string, token: string) {
  return postJson<CurrentUser>("/dashboard/api/auth/login", { username, token })
}

// 登出:清 cookie
export function logout() {
  return postJson<{ ok: boolean }>("/dashboard/api/auth/logout", {})
}

// 当前用户(401 → null,未登录)
export function useCurrentUser() {
  // /me 查询;401 视为未登录(返回 null 而非抛错)
  return useQuery({
    queryKey: ["me"],
    queryFn: async () => {
      try {
        return await getJson<CurrentUser>("/dashboard/api/me")
      } catch {
        return null
      }
    },
    // 会话数据稳定,长 stale
    staleTime: Infinity,
    // 401 不重试
    retry: false,
  })
}
