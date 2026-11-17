// 根路由:全局文档(shellComponent)+ SSR 会话解析 + 主题/暖场脚本。
// 登录门在 /agents 布局(RequireLogin);根本身不设门,使 /login 与 shell 可用。
import {
  HeadContent,
  Outlet,
  Scripts,
  createRootRouteWithContext,
  useHydrated,
  useRouter,
  useRouterState,
} from "@tanstack/react-router"
import { QueryClientProvider } from "@tanstack/react-query"
import type { QueryClient } from "@tanstack/react-query"

import appCss from "../styles.css?url"
import { AppCommandProvider } from "@/lib/appCommands"
import { resolveSessionOnServer } from "@/lib/session-ssr"
import { ThemeSync } from "@/lib/ThemeSync"
import { apiWarmupScript } from "@/features/agents/lib/apiWarmup"

const themeInitScript = `(function(){try{var t=localStorage.getItem("open-swe-theme");var d=t==="dark"||((!t||t==="system")&&window.matchMedia("(prefers-color-scheme: dark)").matches);var r=document.documentElement;r.classList.toggle("dark",d);r.style.colorScheme=d?"dark":"light";}catch(e){}})();`

export const Route = createRootRouteWithContext<{
  queryClient: QueryClient
}>()({
  beforeLoad: ({ context, location }) =>
    resolveSessionOnServer(context.queryClient, location.href),
  head: () => ({
    meta: [
      { charSet: "utf-8" },
      {
        name: "viewport",
        content: "width=device-width, initial-scale=1, maximum-scale=1",
      },
      { name: "theme-color", content: "#1c1c1c" },
      { title: "Tailorbird" },
    ],
    links: [{ rel: "stylesheet", href: appCss }],
  }),
  notFoundComponent: () => (
    <main className="container mx-auto p-4 pt-16">
      <h1 className="text-2xl font-medium">404</h1>
      <p className="text-muted-foreground">
        The requested page could not be found.
      </p>
    </main>
  ),
  shellComponent: RootDocument,
})

function RootDocument({ children }: { children: React.ReactNode }) {
  const { queryClient } = useRouter().options.context
  const pathname = useRouterState({ select: (s) => s.location.pathname })
  // 内联脚本只在 SSR 首帧(及水合首遍)渲染:它们靠 HTML 解析期执行,
  // 客户端水合后再由 React 挂 <script> 节点会触发 "Encountered a script tag"
  // 警告(客户端渲染的 script 永不执行,框架的 HeadContent/Asset 同样按
  // hydrated 后不渲染处理)。水合完成后把节点从树里移除,SSR 副本早已执行。
  const hydrated = useHydrated()
  const warmupScript = hydrated ? null : apiWarmupScript(pathname)
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <head>
        {!hydrated && (
          <script
            suppressHydrationWarning={true}
            dangerouslySetInnerHTML={{ __html: themeInitScript }}
          />
        )}
        {warmupScript && (
          <script
            suppressHydrationWarning={true}
            dangerouslySetInnerHTML={{ __html: warmupScript }}
          />
        )}
        <HeadContent />
      </head>
      <body>
        <ThemeSync />
        <QueryClientProvider client={queryClient}>
          <AppCommandProvider>{children ?? <Outlet />}</AppCommandProvider>
        </QueryClientProvider>
        <Scripts />
      </body>
    </html>
  )
}
