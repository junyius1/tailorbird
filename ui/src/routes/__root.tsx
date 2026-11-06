// 根路由:全局文档(shellComponent:HTML 骨架)+ 登录门 + Query Provider。
// 未登录 → /login;/login 页自身放行。
import {
  HeadContent,
  Outlet,
  Scripts,
  createRootRouteWithContext,
  useNavigate,
  useRouter,
  useRouterState,
} from "@tanstack/react-router"
import { QueryClientProvider } from "@tanstack/react-query"
import type { QueryClient } from "@tanstack/react-query"
import { useEffect } from "react"

import { useCurrentUser } from "@/lib/session"

import appCss from "../styles.css?url"

// 登录门:未登录(401)统一跳 /login;/login 页自身放行。
function RequireAuth() {
  const navigate = useNavigate()
  const pathname = useRouterState({ select: (s) => s.location.pathname })
  const { data: me, isLoading } = useCurrentUser()
  useEffect(() => {
    if (!isLoading && me === null && pathname !== "/login") {
      void navigate({ to: "/login" })
    }
  }, [isLoading, me, pathname, navigate])
  if (isLoading) return null
  return <Outlet />
}

// 根文档(shellComponent):HTML 骨架 + Query Provider + 登录门。
function RootDocument({ children }: { children: React.ReactNode }) {
  // 从路由选项读 queryClient 上下文
  const { queryClient } = useRouter().options.context
  return (
    <html lang="zh-CN">
      <body>
        <HeadContent />
        <QueryClientProvider client={queryClient}>
          {children ?? <RequireAuth />}
        </QueryClientProvider>
        <Scripts />
      </body>
    </html>
  )
}

// 根路由(带 queryClient 上下文)
export const Route = createRootRouteWithContext<{
  queryClient: QueryClient
}>()({
  head: () => ({
    meta: [
      { charSet: "utf-8" },
      { name: "viewport", content: "width=device-width, initial-scale=1" },
      { title: "Tailorbird" },
    ],
    links: [{ rel: "stylesheet", href: appCss }],
  }),
  notFoundComponent: () => (
    <main style={{ padding: 40 }}>
      <h1>404</h1>
      <p className="muted">页面不存在。</p>
    </main>
  ),
  // shell:整页文档;children = 匹配的子路由
  shellComponent: RootDocument,
})
