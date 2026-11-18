// vitest 全局 setup(测试环境清理 + 存储兜底)。
import { JSDOM } from "jsdom"
import { cleanup } from "@testing-library/react"
import { afterEach } from "vitest"

// 新 Node 自带实验性 localStorage 全局(读到 undefined),jsdom 的同名全局
// 不会覆盖它;这里统一兜底,保证 zustand persist / localStorage 可用。
if (!(globalThis as { localStorage?: Storage }).localStorage) {
  const { window } = new JSDOM("", { url: "http://localhost:3000" })
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: window.localStorage,
  })
}

// 每个测试后清理 DOM
afterEach(() => {
  cleanup()
})
