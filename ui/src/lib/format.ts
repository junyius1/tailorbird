// 纯函数:文本/分支格式化工具(无副作用,便于单测)。

// 一条消息的 content 可能是字符串或文本块数组;规范成纯文本。
export interface MsgLike {
  content?: string | Array<{ type: string; text?: string }>
}

// 提取消息正文文本
export function messageText(m: MsgLike): string {
  const c = m.content
  if (typeof c === "string") return c
  if (!Array.isArray(c)) return ""
  return c
    .filter((b) => b.type === "text")
    .map((b) => b.text ?? "")
    .join("")
}

// 标题 → 分支 slug(与 dashboard 一致:local/<slug>)
export function slugify(title: string): string {
  return (
    title
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 40) || "thread"
  )
}
