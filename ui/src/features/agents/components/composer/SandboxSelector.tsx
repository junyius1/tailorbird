import { useEffect, useRef, useState } from "react"
import { CaretDownIcon, SquaresFourIcon } from "@phosphor-icons/react"

import { cn } from "@/lib/utils"

export type SandboxType = "docker" | "local"

interface SandboxSelectorProps {
  /** "default" = 跟随服务配置;docker/local = 本次 run 强制指定 */
  value: "default" | SandboxType
  onChange: (next: "default" | SandboxType) => void
  disabled?: boolean
}

const OPTIONS: Array<{ value: "default" | SandboxType; label: string }> = [
  { value: "default", label: "Default sandbox" },
  { value: "docker", label: "Docker" },
  { value: "local", label: "Local (no isolation)" },
]

/**
 * Per-run sandbox provider picker (default / docker / local).
 *
 * "default" follows the server's SANDBOX_TYPE; an explicit choice is sent as
 * `configurable.sandbox_type` for this run only.
 */
export function SandboxSelector({
  value,
  onChange,
  disabled = false,
}: SandboxSelectorProps) {
  const [open, setOpen] = useState(false)
  const dropdownRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      const target = e.target as Node
      if (dropdownRef.current && !dropdownRef.current.contains(target)) {
        setOpen(false)
      }
    }
    document.addEventListener("mousedown", handleClickOutside)
    return () => document.removeEventListener("mousedown", handleClickOutside)
  }, [])

  const selected = OPTIONS.find((o) => o.value === value)

  return (
    <div ref={dropdownRef} className="relative min-w-0 shrink" title={value === "local" ? "No isolation: commands run directly on the host" : "Sandbox provider for this run"}>
      <button
        type="button"
        disabled={disabled}
        aria-label="Sandbox"
        onClick={() => setOpen((v) => !v)}
        className="flex max-w-[200px] cursor-pointer items-center gap-1 text-muted-foreground transition-opacity hover:opacity-80 disabled:cursor-default disabled:opacity-60"
      >
        <SquaresFourIcon className="size-3.5 shrink-0" />
        <span className="flex-1 truncate text-left">
          {selected?.label ?? "Default sandbox"}
        </span>
        <CaretDownIcon className="size-3 shrink-0 opacity-70" />
      </button>
      {open && (
        <div className="absolute top-full left-0 z-50 mt-1 flex w-56 flex-col rounded border border-border bg-popover text-xs text-popover-foreground shadow-lg">
          {OPTIONS.map((opt) => {
            const isSelected = opt.value === value
            return (
              <button
                key={opt.value}
                type="button"
                onClick={() => {
                  onChange(opt.value)
                  setOpen(false)
                }}
                className={cn(
                  "flex w-full items-center px-2 py-1.5 text-left transition-colors hover:bg-muted",
                  isSelected ? "text-foreground" : "text-muted-foreground"
                )}
              >
                <span className="truncate">{opt.label}</span>
                {isSelected && (
                  <span className="ml-auto pl-3 text-muted-foreground">✓</span>
                )}
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}
