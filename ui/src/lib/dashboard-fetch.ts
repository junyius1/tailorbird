import { createIsomorphicFn } from "@tanstack/react-start"
import { getRequestHeader } from "@tanstack/react-start/server"

import { dashboardApiBase } from "./api-base"

/**
 * Origin for dashboard API requests. The browser uses a relative base so calls
 * stay same-origin; a server render has no relative base to resolve against and
 * goes straight to the backend.
 */
// 本地默认:与 vite dev proxy / 生产 nitro handler 的上游一致(127.0.0.1:8002)。
// 缺了它,SSR 的服务端 origin 为空 → /me 走相对 URL → 抛错 → 会话判为未登录,
// 已登录用户也会被 307 回 /login(登录闪屏)。
const DEFAULT_DASHBOARD_API_URL = "http://127.0.0.1:8002"

export const dashboardRequestOrigin = createIsomorphicFn()
  .client(() => dashboardApiBase())
  .server(() =>
    (process.env.DASHBOARD_API_URL ?? DEFAULT_DASHBOARD_API_URL).replace(
      /\/$/,
      ""
    )
  )

/**
 * `credentials: "include"` means nothing on the server, so the session cookie
 * has to be copied off the incoming request by hand.
 */
export const dashboardForwardedHeaders = createIsomorphicFn()
  .client((): Record<string, string> => ({}))
  .server((): Record<string, string> => {
    const cookie = getRequestHeader("cookie")
    return cookie ? { cookie } : {}
  })

export function dashboardApiUrl(path: string): string {
  return `${dashboardRequestOrigin()}/dashboard/api${path}`
}
