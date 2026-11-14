import { HaaSClient, type RunResponse, type RunStepsResponse, type RunUsageResponse, type Thread } from '@haas/sdk';
export type RunDetailsSdk = Pick<HaaSClient, 'getRun' | 'getRunSteps' | 'getRunUsage' | 'getThread'>;
export interface RunDetailsOverview { run?: RunResponse; steps?: RunStepsResponse; usage?: RunUsageResponse }
export interface PublicToolHistoryEntry { toolName: string; toolCallId?: string; content: string }
export interface ThreadHistoryCursor { offset: number; historyVersion?: number; page: number }
export interface ThreadHistoryPage { entries: PublicToolHistoryEntry[]; next?: ThreadHistoryCursor }
export interface RunDetailsApi { overview(runId: string): Promise<RunDetailsOverview>; historyPage(threadId: string, cursor?: ThreadHistoryCursor): Promise<ThreadHistoryPage> }
const MAX_HISTORY_PAGES = 100, MAX_ITEM_BYTES = 16 * 1024;
const SECRET_KEY = /(?:secret|token|password|api[_-]?key|authorization|credential)/iu;
function safePublicValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.slice(0, 100).map(safePublicValue);
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).slice(0, 100).map(([key, child]) =>
      [key, SECRET_KEY.test(key) ? '[redacted]' : safePublicValue(child)]));
  }
  if (typeof value !== 'string') return value;
  return value
    .replace(/\bBearer\s+[A-Za-z0-9._~+/-]+=*/giu, '[redacted]')
    .replace(/\b(api[_-]?key|token|secret|password|authorization)\b(\s*[:=]\s*)["']?[^,\s"']+/giu, '$1$2[redacted]');
}
export function safePublicText(value: unknown, limit = MAX_ITEM_BYTES): string {
  const safe = safePublicValue(value);
  let text: string;
  try { text = typeof safe === 'string' ? safe : JSON.stringify(safe) ?? String(safe); }
  catch { text = '[unavailable]'; }
  const bytes = new TextEncoder().encode(text);
  if (bytes.length <= limit) return text;
  const marker = limit >= 22 ? '\n[truncated at 16KiB]' : '[truncated]';
  const markerBytes = new TextEncoder().encode(marker).length;
  let end = Math.max(0, limit - markerBytes);
  while (end > 0 && (bytes[end] & 0xc0) === 0x80) end--;
  const bounded = `${new TextDecoder().decode(bytes.slice(0, end))}${marker}`;
  if (new TextEncoder().encode(bounded).length > limit) throw new Error('Public text exceeded its display byte limit.');
  return bounded;
}
interface ThreadHistoryResponse extends Thread {
  items?: unknown;
  next_offset?: unknown;
}
function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}
function publicToolEntry(item: unknown): PublicToolHistoryEntry | undefined {
  if (!isRecord(item) || item.role !== 'tool') return undefined;
  return { toolName: typeof item.name === 'string' ? safePublicText(item.name, 200) : 'Public tool result',
    ...(typeof item.tool_call_id === 'string' ? { toolCallId: safePublicText(item.tool_call_id, 200) } : {}),
    content: safePublicText(item.content) };
}
export function createRunDetailsApi(client: RunDetailsSdk): RunDetailsApi {
  return {
    async overview(runId) {
      const [run, steps, usage] = await Promise.allSettled([client.getRun(runId), client.getRunSteps(runId), client.getRunUsage(runId)]);
      return {
        ...(run.status === 'fulfilled' && run.value.run_id === runId ? { run: run.value } : {}),
        ...(steps.status === 'fulfilled' ? { steps: steps.value } : {}),
        ...(usage.status === 'fulfilled' && usage.value.run_id === runId ? { usage: usage.value } : {}),
      };
    },
    async historyPage(threadId, cursor = { offset: 0, page: 0 }) {
      if (cursor.page >= MAX_HISTORY_PAGES) throw new Error('Public history page limit reached.');
      const response = await client.getThread(threadId, {
        offset: cursor.offset,
        ...(cursor.historyVersion === undefined ? {} : { history_version: cursor.historyVersion }),
      }) as ThreadHistoryResponse;
      if (response.thread_id !== threadId) throw new Error('Public thread history identity mismatch.');
      if (!Number.isSafeInteger(response.history_version) || response.history_version < 0 ||
        (cursor.historyVersion !== undefined && response.history_version !== cursor.historyVersion) || !Array.isArray(response.items)) {
        throw new Error('Invalid public thread history page.');
      }
      const nextOffset = response.next_offset;
      if (nextOffset !== null && (typeof nextOffset !== 'number' || !Number.isSafeInteger(nextOffset) || nextOffset <= cursor.offset)) {
        throw new Error('Invalid public thread history cursor.');
      }
      return {
        entries: response.items.slice(0, 100).flatMap(item => { const entry = publicToolEntry(item); return entry ? [entry] : []; }),
        ...(nextOffset === null ? {} : { next: { offset: nextOffset as number, historyVersion: response.history_version, page: cursor.page + 1 } }),
      };
    },
  };
}
export function createBrowserRunDetailsApi(): RunDetailsApi {
  return createRunDetailsApi(new HaaSClient({ baseUrl: window.location.origin, maxRetries: 0 }));
}
