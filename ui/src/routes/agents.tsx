// agents 布局:侧栏(线程列表 + 新建)+ 主区(Outlet)。
// 一个持久 StreamProvider 覆盖整个 /agents 子树(懒创建线程)。
import { useEffect } from "react"
import { Outlet, createFileRoute, useRouterState } from "@tanstack/react-router"

import { AgentsShell } from "@/features/agents/components/AgentsSidebar"
import { Skeleton } from "@/components/ui/skeleton"
import { AgentThreadStreamProvider } from "@/features/agents/lib/AgentThreadStreamProvider"
import { RequireLogin } from "@/lib/auth-redirect"
import { useSession } from "@/lib/session"

export const Route = createFileRoute("/agents")({
  component: AgentsLayout,
})

/**
 * `.agents-ui` 给布局子树定主题,但弹层/菜单 portal 到 `<body>`,
 * 挂载时标记文档根保持同色板。
 */
function useAgentsTheme() {
  useEffect(() => {
    document.documentElement.dataset["agentsTheme"] = "true"
    return () => {
      delete document.documentElement.dataset["agentsTheme"]
    }
  }, [])
}

function AgentsLayout() {
  useAgentsTheme()
  const session = useSession()
  const pathname = useRouterState({
    select: (state) => state.location.pathname,
  })
  const [, section, threadId, nestedRoute] = pathname.split("/")
  const activeThreadId =
    section === "agents" &&
    threadId &&
    nestedRoute !== "plan" &&
    threadId !== "automations" &&
    threadId !== "skills" &&
    threadId !== "threads" &&
    threadId !== "reviews" &&
    threadId !== "local"
      ? threadId
      : undefined

  if (session.isLoading) {
    return (
      <main className="agents-ui flex h-svh items-center justify-center bg-background p-6">
        <Skeleton className="h-40 w-full max-w-md" />
      </main>
    )
  }

  if (!session.data) return <RequireLogin />

  return (
    <AgentsShell
      user={session.data}
      localOnly={false}
      activeThreadId={activeThreadId}
      activeLocalSessionId={undefined}
    >
      <AgentThreadStreamProvider threadId={activeThreadId ?? null}>
        <Outlet />
      </AgentThreadStreamProvider>
    </AgentsShell>
  )
}
