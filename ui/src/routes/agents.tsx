// agents 布局:侧栏(线程列表 + 新建)+ 主区(Outlet)。
import { createFileRoute, useNavigate, Outlet } from "@tanstack/react-router"
import { useQuery } from "@tanstack/react-query"
import { useState } from "react"

import { getJson } from "@/lib/api"
import { logout, useCurrentUser } from "@/lib/session"

// 线程列表项(dashboard /threads 对齐)
export interface ThreadListItem {
  id: string
  title: string
  repo: string | null
  branch: string | null
  model: string | null
  status: string
  isOwner: boolean
  updatedAt: string | null
}

// threads 响应
interface ThreadsResponse {
  items: ThreadListItem[]
  hasMore: boolean
}

// agents 布局路由
export const Route = createFileRoute("/agents")({
  component: AgentsLayout,
})

// 布局组件:侧栏 + 主区
function AgentsLayout() {
  const navigate = useNavigate()
  const { data: me } = useCurrentUser()
  const [repo, setRepo] = useState("")
  // 侧栏线程列表
  const threads = useQuery({
    queryKey: ["threads"],
    queryFn: () => getJson<ThreadsResponse>("/dashboard/api/threads?limit=50"),
  })

  // 新建线程:跳到 /agents/new
  function onNew() {
    void navigate({ to: "." })
  }

  // 登出
  async function onLogout() {
    try {
      await logout()
    } catch {
      // 忽略登出错误
    }
    window.location.assign("/login")
  }

  const items = threads.data?.items ?? []

  return (
    <div className="layout">
      <aside className="sidebar">
        <div style={{ padding: 14 }} className="row">
          <strong>Tailorbird</strong>
          <div className="spacer" />
          <button className="ghost" onClick={onNew}>
            新建
          </button>
        </div>
        <div style={{ padding: "0 14px 8px" }}>
          <input
            value={repo}
            onChange={(e) => setRepo(e.target.value)}
            placeholder="仓库 owner/name(可选)"
          />
        </div>
        <div style={{ padding: "4px 8px" }}>
          {items.map((t) => (
            <div
              key={t.id}
              className="thread-item"
              onClick={() => void navigate({ to: "/agents/$threadId", params: { threadId: t.id } })}
            >
              <div className="row">
                <span>{t.title || "(未命名)"}</span>
                <div className="spacer" />
                <span className={`badge ${t.status === "running" ? "running" : t.status === "error" ? "error" : ""}`}>
                  {t.status}
                </span>
              </div>
              <div className="muted" style={{ fontSize: 12 }}>
                {t.branch ?? "未设分支"}
              </div>
            </div>
          ))}
          {items.length === 0 ? (
            <p className="muted" style={{ padding: "8px 4px" }}>
              还没有线程。点「新建」开始。
            </p>
          ) : null}
        </div>
        <div
          style={{
            position: "sticky",
            bottom: 0,
            padding: 12,
            borderTop: "1px solid var(--border)",
            background: "var(--panel)",
          }}
          className="row"
        >
          <span className="muted">{me?.name ?? me?.sub ?? ""}</span>
          <div className="spacer" />
          <button className="ghost" onClick={onLogout}>
            登出
          </button>
        </div>
      </aside>
      <main className="main">
        <Outlet />
      </main>
    </div>
  )
}
