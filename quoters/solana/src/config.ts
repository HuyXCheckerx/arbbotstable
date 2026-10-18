import { Connection } from "@solana/web3.js";
import { rpcFetch, type FetchRetryOptions } from "./http.js";

/**
 * RPC configuration for the quoter: the connection helpers of solanaarbitrage's src/config.ts, without the
 * trading bot's settings (flash loans, Jito tips, aggregator providers), which the quoter never reads.
 */
export interface Config {
  rpcUrl: string;
  /** Helius endpoint, used for program-account scans: HELIUS_RPC_URL, or SOLANA_RPC_URL when that is itself Helius. */
  heliusUrl?: string;
  /** Websocket endpoint for live account/slot updates: SOLANA_WS_URL, else Helius's, else derived from SOLANA_RPC_URL. */
  wsUrl: string;
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): Config {
  return {
    rpcUrl: env.SOLANA_RPC_URL || "https://api.mainnet-beta.solana.com",
    heliusUrl: env.HELIUS_RPC_URL || (/helius-rpc\.com/.test(env.SOLANA_RPC_URL ?? "") ? env.SOLANA_RPC_URL : undefined),
    wsUrl: env.SOLANA_WS_URL || (env.HELIUS_RPC_URL || env.SOLANA_RPC_URL || "https://api.mainnet-beta.solana.com").replace(/^http/, "ws"),
  };
}

/**
 * HTTP JSON-RPC with failover: a network error, timeout, 429 or 5xx from one endpoint retries the
 * same request on the next (SOLANA_RPC_URL_BACKUP, comma-separated). Websockets stay on the primary.
 */
export function failoverFetch(endpoints: string[], options: FetchRetryOptions = {}): typeof fetch {
  return rpcFetch(endpoints, options);
}

export function rpcEndpoints(config: Config, env: NodeJS.ProcessEnv = process.env): string[] {
  const explicit = (env.SOLANA_RPC_URL_BACKUP ?? "").split(",").map((u) => u.trim()).filter(Boolean);
  // Reuse an already configured Helius service by default; explicit backups override this fallback.
  const backups = explicit.length ? explicit : config.heliusUrl ? [config.heliusUrl] : [];
  return [...new Set([config.rpcUrl, ...backups])];
}

export function connect(config: Config, env: NodeJS.ProcessEnv = process.env): Connection {
  const timeoutMs = Number(env.RPC_TIMEOUT_MS ?? "20000");
  const maxAttempts = Number(env.RPC_MAX_ATTEMPTS ?? "3");
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1) throw new Error("RPC_TIMEOUT_MS must be a positive integer");
  if (!Number.isInteger(maxAttempts) || maxAttempts < 1 || maxAttempts > 10) throw new Error("RPC_MAX_ATTEMPTS must be an integer between 1 and 10");
  return new Connection(config.rpcUrl, { commitment: "confirmed", disableRetryOnRateLimit: true,
    wsEndpoint: config.wsUrl, fetch: failoverFetch(rpcEndpoints(config, env), { timeoutMs, maxAttempts }) });
}

/** The Helius endpoint, or a clear error naming the feature that needs it. */
export function heliusUrl(config: Config, feature: string): string {
  if (!config.heliusUrl) throw new Error(`${feature} needs Helius: set HELIUS_RPC_URL (your Helius URL with ?api-key=) in .env`);
  return config.heliusUrl;
}

/** Strip credentials (RPC URL, API keys) from messages before they reach logs or reports. */
export function redact(message: string, env: NodeJS.ProcessEnv = process.env): string {
  for (const secret of [env.SOLANA_RPC_URL, env.HELIUS_RPC_URL, env.SOLANA_WS_URL, ...(env.SOLANA_RPC_URL_BACKUP ?? "").split(","),
    env.JUPITER_API_KEY, env.GEYSER_GRPC_URL, env.GEYSER_X_TOKEN]) {
    if (secret) message = message.replaceAll(secret, "[redacted]");
  }
  // Any API key embedded in a URL (e.g. a Helius key inside a library's error message).
  return message.replace(/(api[-_]?key=)[^&\s"')]+/gi, "$1[redacted]");
}
