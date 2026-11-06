// vitest 全局 setup(测试环境清理)。
import { cleanup } from "@testing-library/react"
import { afterEach } from "vitest"

// 每个测试后清理 DOM
afterEach(() => {
  cleanup()
})
