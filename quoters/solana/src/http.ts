export interface FetchRetryOptions {
  timeoutMs?: number;
  /** Fetch attempts, or complete endpoint rounds for rpcFetch. */
  maxAttempts?: number;
  /** Base delay multiplied by the completed attempt/round number. */
  retryDelayMs?: number;
  fetch?: typeof fetch;
  sleep?: (ms: number) => Promise<void>;
  timeoutSignal?: (ms: number) => AbortSignal;
}

export interface RpcTransportFailure {
  method: string;
  attempts: number;
  rounds: number;
  reason: "timeout" | "network" | "body" | "http";
  status?: number;
}

/** A transient RPC transport failure. Diagnostics deliberately exclude URLs, bodies and raw causes. */
export class RpcTransportError extends Error {
  readonly details: RpcTransportFailure;
  constructor(details: RpcTransportFailure) {
    const method = safeMethod(details.method);
    super(`RPC ${method} transport exhausted after ${details.rounds} round(s), ${details.attempts} request(s): ${details.status === undefined ? details.reason : `HTTP ${details.status}`}`);
    this.name = "RpcTransportError";
    this.details = { ...details, method };
  }
}

/** web3.js only invokes its callback for same-realm Error objects. */
export function normalizeFetchError(error: unknown): Error {
  return error instanceof Error ? error : new Error("HTTP transport failed", { cause: error });
}

function safeMethod(method: unknown): string {
  return typeof method === "string" && /^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(method) ? method : "unknown";
}

function rpcMethod(init: RequestInit): string {
  if (typeof init.body !== "string") return "unknown";
  try {
    const body = JSON.parse(init.body);
    return Array.isArray(body) ? "batch" : safeMethod(body?.method);
  } catch { return "unknown"; }
}

function timing(options: FetchRetryOptions, defaultDelay: number) {
  const timeoutMs = options.timeoutMs ?? 20_000;
  const maxAttempts = options.maxAttempts ?? 3;
  const retryDelayMs = options.retryDelayMs ?? defaultDelay;
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1) throw new Error("HTTP timeout must be a positive integer");
  if (!Number.isInteger(maxAttempts) || maxAttempts < 1 || maxAttempts > 10) throw new Error("HTTP attempts must be an integer between 1 and 10");
  if (!Number.isFinite(retryDelayMs) || retryDelayMs < 0) throw new Error("HTTP retry delay must be non-negative");
  return { timeoutMs, maxAttempts, retryDelayMs };
}

function withSignal<T>(signal: AbortSignal, work: () => Promise<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    const abort = () => reject(normalizeFetchError(signal.reason));
    if (signal.aborted) { abort(); return; }
    signal.addEventListener("abort", abort, { once: true });
    Promise.resolve().then(work).then(resolve, (error) => reject(normalizeFetchError(error)))
      .finally(() => signal.removeEventListener("abort", abort));
  });
}

async function pause(ms: number, signal: AbortSignal | null | undefined, options: FetchRetryOptions): Promise<void> {
  const work = () => options.sleep ? options.sleep(ms) : new Promise<void>((resolve) => setTimeout(resolve, ms));
  if (signal) await withSignal(signal, work);
  else await work();
}

async function fetchAttempt<T>(url: string | URL | Request, init: RequestInit, options: FetchRetryOptions,
  timeoutMs: number, consume: (response: Response) => Promise<T>): Promise<T> {
  const callerSignal = init.signal ?? (url instanceof Request ? url.signal : undefined);
  const timeout = (options.timeoutSignal ?? AbortSignal.timeout)(timeoutMs);
  const signal = callerSignal ? AbortSignal.any([callerSignal, timeout]) : timeout;
  return withSignal(signal, async () => {
    const response = await (options.fetch ?? globalThis.fetch)(url, { ...init, signal });
    return consume(response);
  });
}

export async function retryFetch(url: string | URL | Request, init: RequestInit = {}, options: FetchRetryOptions = {}): Promise<Response> {
  const { timeoutMs, maxAttempts, retryDelayMs } = timing(options, 1000);
  const callerSignal = init.signal ?? (url instanceof Request ? url.signal : undefined);
  for (let attempt = 1; ; attempt++) {
    try {
      return await fetchAttempt(url, init, options, timeoutMs, async (response) => response);
    } catch (error) {
      if (callerSignal?.aborted) throw normalizeFetchError(callerSignal.reason);
      if (attempt >= maxAttempts) throw normalizeFetchError(error);
      await pause(retryDelayMs * attempt, callerSignal, options);
    }
  }
}

class RetryableHttpError extends Error {
  constructor(readonly status: number) { super(`HTTP ${status}`); }
}
class PermanentBodyError extends Error {
  constructor(status: number) { super(`RPC HTTP ${status} response body failed`); }
}

/** Replay JSON-RPC with a body deadline; reuse each method's last successful endpoint. */
export function rpcFetch(endpoints: string[], options: FetchRetryOptions = {}): typeof fetch {
  const { timeoutMs, maxAttempts, retryDelayMs } = timing(options, 250);
  endpoints = [...new Set(endpoints)];
  if (!endpoints.length) throw new Error("RPC transport needs at least one endpoint");
  const affinity = new Map<string, number>();
  return (async (input: string | URL | Request, init: RequestInit = {}) => {
    const callerSignal = init.signal ?? (input instanceof Request ? input.signal : undefined);
    const method = rpcMethod(init);
    const preferred = affinity.get(method) ?? 0;
    const order = [preferred, ...endpoints.map((_, i) => i).filter((i) => i !== preferred)];
    let attempts = 0;
    let failure: Pick<RpcTransportFailure, "reason" | "status"> = { reason: "network" };
    for (let round = 1; round <= maxAttempts; round++) {
      for (const i of order) {
        if (callerSignal?.aborted) throw normalizeFetchError(callerSignal.reason);
        attempts++;
        let phase: "network" | "body" = "network";
        let permanentStatus: number | undefined;
        try {
          const response = await fetchAttempt(i === 0 ? input : endpoints[i], init, options, timeoutMs, async (response) => {
            if (response.status === 429 || response.status === 402 || response.status >= 500) {
              // 402 = provider out of compute units: fail over to the next endpoint, do not treat as permanent.
              response.body?.cancel().catch(() => undefined);
              throw new RetryableHttpError(response.status);
            }
            phase = "body";
            if (response.status >= 400 && response.status < 500) permanentStatus = response.status;
            let body: ArrayBuffer;
            try { body = await response.arrayBuffer(); }
            catch (error) {
              if (response.status >= 400 && response.status < 500) throw new PermanentBodyError(response.status);
              throw error;
            }
            return new Response(body.byteLength ? body : null, {
              status: response.status, statusText: response.statusText, headers: response.headers,
            });
          });
          // Unknown requests and mixed batches must not share affinity across unrelated methods.
          if (response.ok && method !== "unknown" && method !== "batch") affinity.set(method, i);
          return response;
        } catch (error) {
          if (callerSignal?.aborted) throw normalizeFetchError(callerSignal.reason);
          if (error instanceof PermanentBodyError) throw error;
          if (permanentStatus !== undefined) throw new PermanentBodyError(permanentStatus);
          failure = error instanceof RetryableHttpError ? { reason: "http", status: error.status }
            : { reason: normalizeFetchError(error).name === "TimeoutError" ? "timeout" : phase };
        }
      }
      if (round < maxAttempts) await pause(retryDelayMs * round, callerSignal, options);
    }
    throw new RpcTransportError({ method, attempts, rounds: maxAttempts, ...failure });
  }) as typeof fetch;
}

export async function json<T>(url: string, init: RequestInit = {}): Promise<T> {
  for (let attempt = 0; attempt < 4; attempt++) {
    const response = await retryFetch(url, init);
    if (response.ok) return response.json() as Promise<T>;
    if ((response.status === 429 || response.status === 402 || response.status >= 500) && attempt < 3) {
      await new Promise((resolve) => setTimeout(resolve, 2500 * (attempt + 1)));
      continue;
    }
    throw new Error(`HTTP ${response.status}: ${(await response.text()).slice(0, 400)}`);
  }
  throw new Error("Request retries exhausted");
}
