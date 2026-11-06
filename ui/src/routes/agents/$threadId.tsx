// 线程详情页:对话(transcript)+ 输入框 + 本地 git diff 面板 + 推理懒加载。
// 数据源:dashboard /threads/{id}/state(轮询)/ /reasoning(按需)/ /working-tree-diff。
import { createFileRoute, useParams } from "@tanstack/react-router"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"

import { getJson, postJson } from "@/lib/api"
import { messageText } from "@/lib/format"

// 线程详情路由
export const Route = createFileRoute("/agents/$threadId")({
  component: ThreadDetailPage,
})

// 一条消息(state.values.messages 元素)
interface Msg {
  type: string
  content: string | Array<{ type: string; text?: string }>
  additional_kwargs?: Record<string, unknown>
}

// state 响应
interface StateResponse {
  values?: { messages?: Msg[] }
  metadata?: Record<string, unknown>
}

// 工作树 diff 响应
interface WorkingTreeDiff {
  diff: string
  branch: string | null
  status: string
}

// 推理清单元素
interface ReasoningItem {
  message_id: string
  content_length: number | null
}

// 推理正文响应
interface ReasoningDetail {
  message_id: string
  content: string
}

// 轮询间隔:run 活跃时更快
const POLL_MS = 2500

// 线程详情页
function ThreadDetailPage() {
  const { threadId } = useParams({ from: "/agents/$threadId" })
  const queryClient = useQueryClient()
  // 输入框文本
  const [input, setInput] = useState("")
  // 正在发送
  const [sending, setSending] = useState(false)
  // 发送错误
  const [sendError, setSendError] = useState<string | null>(null)
  // 展开的推理 message_id 集合
  const [openReasoning, setOpenReasoning] = useState<Record<string, string | null>>({})

  // state(轮询;活跃时 2.5s,idle 也轮询以捕获新 run)
  const stateQ = useQuery({
    queryKey: ["thread-state", threadId],
    queryFn: () => getJson<StateResponse>(`/dashboard/api/threads/${threadId}/state`),
    refetchInterval: POLL_MS,
    refetchIntervalInBackground: false,
    retry: 1,
  })

  const messages = stateQ.data?.values?.messages ?? []
  const metadata = stateQ.data?.metadata ?? {}
  const status = String(metadata.status ?? "idle")

  // 工作树 diff(本地 git 能力;30s 轮询)
  const wtQ = useQuery({
    queryKey: ["wt-diff", threadId],
    queryFn: () => getJson<WorkingTreeDiff>(`/dashboard/api/threads/${threadId}/working-tree-diff`),
    refetchInterval: 30_000,
    retry: 1,
  })

  // 推理清单(打开时拉)
  const reasoningQ = useQuery({
    queryKey: ["reasoning-list", threadId],
    queryFn: () => getJson<ReasoningItem[]>(`/dashboard/api/threads/${threadId}/reasoning`),
    enabled: true,
    staleTime: 15_000,
    retry: 1,
  })
  const reasoningByMsg = new Map<string, ReasoningItem>()
  for (const r of reasoningQ.data ?? []) reasoningByMsg.set(r.message_id, r)

  // 展开/收起某条推理(懒加载正文)
  async function toggleReasoning(mid: string) {
    if (mid in openReasoning) {
      setOpenReasoning((p) => {
        const n = { ...p }
        delete n[mid]
        return n
      })
      return
    }
    setOpenReasoning((p) => ({ ...p, [mid]: null }))
    try {
      const d = await getJson<ReasoningDetail>(`/dashboard/api/threads/${threadId}/reasoning/${mid}`)
      setOpenReasoning((p) => ({ ...p, [mid]: d.content }))
    } catch {
      setOpenReasoning((p) => ({ ...p, [mid]: "(无推理记录)" }))
    }
  }

  // 发送:忙 → messages 入队;idle → run.start
  async function onSend(e: React.FormEvent) {
    e.preventDefault()
    if (!input.trim()) return
    setSending(true)
    setSendError(null)
    const text = input
    try {
      // 先尝试入队(busy 线程)
      let queued = false
      try {
        await postJson(`/dashboard/api/threads/${threadId}/messages`, {
          content: text,
          plan_mode: Boolean(metadata.plan_mode),
        })
        queued = true
      } catch (err) {
        // 409 = idle,需要 run.start
        if ((err as { status?: number })?.status !== 409) throw err
      }
      if (!queued) {
        await postJson(`/dashboard/api/threads/${threadId}/commands`, {
          method: "run.start",
          params: {
            input: { messages: [{ role: "user", content: text }] },
            config: { configurable: {} },
          },
        })
      }
      setInput("")
      queryClient.invalidateQueries({ queryKey: ["thread-state", threadId] })
    } catch (err) {
      setSendError(err instanceof Error ? err.message : "发送失败")
    } finally {
      setSending(false)
    }
  }

  const diff = wtQ.data?.diff ?? ""
  const diffBranch = wtQ.data?.branch ?? metadata.branch
  const diffStatus = wtQ.data?.status ?? "unknown"

  return (
    <div className="main">
      {/* 头部:标题 + 分支 + 状态 */}
      <div
        style={{
          padding: "10px 20px",
          borderBottom: "1px solid var(--border)",
          display: "flex",
          gap: 10,
          alignItems: "center",
        }}
      >
        <strong>{String(metadata.title ?? "线程")}</strong>
        <span className="mono muted">{String(diffBranch ?? "")}</span>
        <div className="spacer" />
        <span className={`badge ${status === "running" ? "running" : status === "error" ? "error" : ""}`}>
          {status}
        </span>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 380px", flex: 1, minHeight: 0 }}>
        {/* 左:对话 */}
        <div className="transcript" style={{ borderRight: "1px solid var(--border)" }}>
          {messages.map((m, i) => {
            const who = m.type === "human" ? "你" : m.type === "ai" ? "agent" : m.type
            const isAi = m.type === "ai"
            const mid = (m as { id?: string }).id
            const hasReasoning = mid ? reasoningByMsg.has(mid) : false
            const reasoningOpen = mid ? openReasoning[mid] : undefined
            return (
              <div key={i} className={`msg ${who === "你" ? "user" : isAi ? "ai" : "system"}`}>
                <div className="who">{who}</div>
                <div>{messageText(m) || <span className="muted">(空)</span>}</div>
                {isAi && hasReasoning && mid ? (
                  <div className="reasoning" style={{ marginTop: 10 }}>
                    <div
                      className="row"
                      style={{ cursor: "pointer", fontSize: 12.5, color: "var(--muted)" }}
                      onClick={() => void toggleReasoning(mid)}
                    >
                      <span>▸ 推理({reasoningByMsg.get(mid)?.content_length ?? "?"} 字符)</span>
                    </div>
                    {reasoningOpen !== undefined ? (
                      <pre className="mono" style={{ margin: "8px 0 0", whiteSpace: "pre-wrap" }}>
                        {reasoningOpen}
                      </pre>
                    ) : null}
                  </div>
                ) : null}
              </div>
            )
          })}
          {messages.length === 0 ? (
            <p className="muted">还没有消息。</p>
          ) : null}
        </div>

        {/* 右:本地 git diff */}
        <div style={{ overflow: "auto", padding: 16 }}>
          <h3 style={{ marginTop: 0, fontSize: 15 }}>本地 git diff</h3>
          <p className="muted" style={{ fontSize: 12.5 }}>
            分支 <span className="mono">{String(diffBranch ?? "—")}</span> · 状态{" "}
            <span className="mono">{diffStatus}</span>
          </p>
          {diff ? (
            <pre className="diff">{diff}</pre>
          ) : (
            <p className="muted">工作树无改动。</p>
          )}
        </div>
      </div>

      {/* 底部:输入框 */}
      <form className="composer" onSubmit={onSend}>
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="输入消息…(Enter 发送,Shift+Enter 换行)"
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault()
              void onSend(e)
            }
          }}
        />
        <button type="submit" disabled={sending || !input.trim()}>
          {sending ? "发送中…" : "发送"}
        </button>
      </form>
      {sendError ? (
        <div style={{ padding: "0 16px 10px", color: "var(--danger)" }}>{sendError}</div>
      ) : null}
    </div>
  )
}
