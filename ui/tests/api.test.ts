// api.ts 错误处理单测(不依赖网络:用 fetch mock)。
import { afterEach, describe, expect, it, vi } from "vitest"

import { ApiError, getJson } from "../src/lib/api"

// 每个测试后重置 fetch mock
afterEach(() => {
  vi.restoreAllMocks()
})

// 非 2xx → ApiError(带 status)
describe("api.request", () => {
  it("401 抛 ApiError(status=401)", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ detail: "unauthenticated" }), {
        status: 401,
        headers: { "content-type": "application/json" },
      })
    )
    await expect(getJson("/x")).rejects.toMatchObject({ status: 401 })
    await expect(getJson("/x")).rejects.toBeInstanceOf(ApiError)
  })
  it("2xx 返回解析后的 JSON", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "content-type": "application/json" },
      })
    )
    await expect(getJson("/x")).resolves.toEqual({ ok: true })
  })
})
