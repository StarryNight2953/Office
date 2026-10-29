export async function request<T>(path: string, body?: unknown, method?: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`/api${path}`, {
    method: method ?? (body === undefined ? 'GET' : 'POST'),
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const error = await response.json();
      message = typeof error.detail === 'string' ? error.detail : JSON.stringify(error.detail ?? error);
    } catch { /* A disconnected service may return HTML. */ }
    throw new Error(message);
  }
  return response.json() as Promise<T>;
}

export function errorMessage(error: unknown): string {
  if (error instanceof TypeError && error.message.includes('fetch')) return '无法连接到实验服务，请确认 Python 服务已启动后重试。';
  return error instanceof Error ? error.message : String(error);
}
