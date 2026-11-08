// 终端会话桩:本地变体无云终端,保留类型面供右侧面板类型检查。
export type TerminalTarget =
  | { kind: "cloud"; threadId: string }
  | { kind: "local"; projectId: string }

export interface TerminalSessionState {
  terminalId: string
  groupId: string
  status: "idle" | "active" | "closed"
  exitCode: number | null
  cwd: string
  cols: number
  rows: number
  updatedAt: number
}

export const EMPTY_TERMINAL_SESSION: TerminalSessionState = Object.freeze({
  terminalId: "",
  groupId: "",
  status: "closed",
  exitCode: null,
  cwd: "",
  cols: 80,
  rows: 24,
  updatedAt: 0,
})
