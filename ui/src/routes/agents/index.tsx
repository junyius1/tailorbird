// agents 首页 = 新建任务:自由文本仓库选择 + 初始消息 + 本地分支。
// 提交 → POST /dashboard/api/threads/{tid}/commands run.start(lazy 建线程)→ 进入线程。
import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { useQueryClient } from "@tanstack/react-query"
import { useState } from "react"

import { postJson } from "@/lib/api"
import { slugify } from "@/lib/format"

// agents 首页路由
export const Route = createFileRoute("/agents/")({
  component: NewTaskPage,
})

// 新建任务页
function NewTaskPage() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  // 仓库(owner/name,自由文本,可空)
  const [repo, setRepo] = useState("")
  // 初始消息
  const [message, setMessage] = useState("")
  // 提交中
  const [busy, setBusy] = useState(false)
  // 错误
  const [error, setError] = useState<string | null>(null)

  // 提交:建线程 + 开 run
  async function onSubmit(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      // 客户端铸造线程 id(带 dash 的规范 uuid)
      const tid =
        typeof crypto !== "undefined" && "randomUUID" in crypto
          ? crypto.randomUUID()
          : `${Date.now()}-${Math.random().toString(16).slice(2)}`
      const title = message.slice(0, 80)
      const branch = `local/${slugify(title)}`
      // run.start 信封(经 dashboard 富化:注入 local_user/source/v2 标记)
      await postJson("/dashboard/api/threads/" + tid + "/commands", {
        method: "run.start",
        params: {
          input: { messages: [{ role: "user", content: message }] },
          config: { configurable: {} },
          metadata: { repo: repo.trim() || undefined, title, branch },
        },
      })
      // 刷新侧栏列表
      queryClient.invalidateQueries({ queryKey: ["threads"] })
      void navigate({ to: "/agents/$threadId", params: { threadId: tid } })
    } catch (err) {
      setError(err instanceof Error ? err.message : "启动失败")
      setBusy(false)
    }
  }

  return (
    <div style={{ padding: 40, maxWidth: 760, margin: "0 auto" }}>
      <h1 style={{ fontSize: 22, marginBottom: 4 }}>新建任务</h1>
      <p className="muted" style={{ marginTop: 0 }}>
        描述你要交给 agent 的任务。它会在本地沙箱里工作,默认只在本地分支
        提交,不推送。
      </p>
      <form onSubmit={onSubmit} className="card" style={{ padding: 20 }}>
        <label style={{ display: "block", marginBottom: 4 }}>
          仓库(owner/name,可选)
        </label>
        <input
          value={repo}
          onChange={(e) => setRepo(e.target.value)}
          placeholder="例如:junyius1/tailorbird"
        />
        <label style={{ display: "block", margin: "14px 0 4px" }}>任务</label>
        <textarea
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          placeholder="例如:在 src 下新增一个 hello 函数并补上测试"
          style={{ minHeight: 140 }}
          required
        />
        {error ? <p style={{ color: "var(--danger)" }}>{error}</p> : null}
        <div style={{ marginTop: 16 }}>
          <button type="submit" disabled={busy || !message.trim()}>
            {busy ? "启动中…" : "启动任务"}
          </button>
        </div>
      </form>
    </div>
  )
}
