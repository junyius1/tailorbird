// Datadog RUM 桩:本地自托管变体不上报,保持与 open-swe 相同的 API 面。
export function getDatadogSessionLink(): string | undefined {
  return undefined
}

export function isDatadogRumInitialized(): boolean {
  return false
}

export function subscribeToDatadogInitialization(cb: () => void): () => void {
  return cb
}

export async function initializeDatadogRum(): Promise<void> {
  // no-op
}
