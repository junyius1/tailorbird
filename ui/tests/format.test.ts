// format.ts 纯函数单测。
import { describe, expect, it } from "vitest"

import { messageText, slugify } from "../src/lib/format"

// slugify:标题 → 分支 slug
describe("slugify", () => {
  it("转小写并把非字母数字折叠为连字符", () => {
    expect(slugify("Hello World! 新增 功能")).toBe("hello-world")
  })
  it("去掉首尾连字符并截断到 40", () => {
    expect(slugify("---a---")).toBe("a")
    expect(slugify("x".repeat(80)).length).toBeLessThanOrEqual(40)
  })
  it("空标题回退 thread", () => {
    expect(slugify("!!!")).toBe("thread")
  })
})

// messageText:字符串 / 文本块数组
describe("messageText", () => {
  it("字符串原样返回", () => {
    expect(messageText({ content: "收到" })).toBe("收到")
  })
  it("只取 text 块并拼接", () => {
    const blocks = [
      { type: "text", text: "a" },
      { type: "tool_call", text: "x" },
      { type: "text", text: "b" },
    ] as Array<{ type: string; text?: string }>
    expect(messageText({ content: blocks })).toBe("ab")
  })
  it("缺 content 返回空串", () => {
    expect(messageText({})).toBe("")
  })
})
