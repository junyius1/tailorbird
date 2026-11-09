// 终端分组控制器桩:本地变体无终端,控制器返回空分组、空操作。
import { useCallback, useState } from "react"
import type { TerminalTarget } from "./terminalSession"

export interface TerminalGroupsController {
  state: {
    terminalGroups: Array<{
      id: string
      target: TerminalTarget
      terminalIds: string[]
      title?: string
    }>
  }
  addGroup: () => string
  closeGroup: (groupId: string) => Promise<boolean>
  focus: (terminalId: string) => void
}

export function useTerminalGroups(
  _target: TerminalTarget,
  _cwd: string,
): TerminalGroupsController {
  const [state] = useState(() => ({ terminalGroups: [] as TerminalGroupsController["state"]["terminalGroups"] }))
  const addGroup = useCallback(() => "", [])
  const closeGroup = useCallback(async () => false, [])
  const focus = useCallback(() => {}, [])
  return { state, addGroup, closeGroup, focus }
}
