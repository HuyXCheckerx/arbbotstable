/**
 * Long-lived host for solana_flash_arb.ts route checks.
 *
 * A one-shot check pays ~1.5 s of Node/tsx startup and SDK imports and opens
 * fresh TLS connections to every RPC and quote endpoint. This worker imports
 * the engine once and runs one check at a time, each with the exact argv and
 * environment a one-shot process would receive. Cross-check state already
 * lives in files under logs/, so results are the same as a fresh process.
 *
 * Protocol: one JSON request per stdin line, {"id", "argv", "env"}. Each
 * response is one stdout line starting with RESPONSE_PREFIX and carrying
 * {"id", "exitCode", "stdout", "stderr"}; a {"ready": true} line is sent once
 * the engine is imported. Engine console output is captured per request.
 */
import dotenv from "dotenv";
import readline from "node:readline";

const RESPONSE_PREFIX = "@@ARBBOT_SOLANA_WORKER@@";

interface WorkerRequest {
  id: number;
  argv: string[];
  env: Record<string, string>;
}

interface ActiveJob {
  id: number;
  stdout: string[];
  stderr: string[];
}

const writeProtocol = process.stdout.write.bind(process.stdout);
const writeDiagnostic = process.stderr.write.bind(process.stderr);
let active: ActiveJob | undefined;

function capture(target: "stdout" | "stderr") {
  return ((chunk: unknown, encoding?: unknown, callback?: unknown): boolean => {
    const text = typeof chunk === "string"
      ? chunk
      : Buffer.from(chunk as Uint8Array).toString(
        typeof encoding === "string" ? (encoding as BufferEncoding) : "utf8",
      );
    // Output outside a request (late timers, warnings) has no reader.
    active?.[target].push(text);
    const done = typeof encoding === "function" ? encoding : callback;
    if (typeof done === "function") queueMicrotask(() => (done as () => void)());
    return true;
  }) as typeof process.stdout.write;
}

process.stdout.write = capture("stdout");
process.stderr.write = capture("stderr");

function respond(job: ActiveJob, exitCode: number): void {
  writeProtocol(
    `${RESPONSE_PREFIX}${JSON.stringify({
      id: job.id,
      exitCode,
      stdout: job.stdout.join(""),
      stderr: job.stderr.join(""),
    })}\n`,
  );
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

// A one-shot process dies on an unhandled error, possibly mid-check. Do the
// same: report the failure and exit so the next check starts from a clean
// process instead of sharing state with a half-finished one.
function fatal(error: unknown): void {
  const job = active;
  if (job) {
    job.stderr.push(`ERROR: ${errorMessage(error)}\n`);
    respond(job, 1);
  }
  process.exit(1);
}
process.on("uncaughtException", fatal);
process.on("unhandledRejection", fatal);

function applyEnvironment(env: Record<string, string>): void {
  for (const key of Object.keys(process.env)) {
    if (!(key in env)) delete process.env[key];
  }
  Object.assign(process.env, env);
  // A one-shot process loads .env without overriding its given environment.
  dotenv.config({ quiet: true });
}

async function runJob(engineMain: (argv: string[]) => Promise<void>, request: WorkerRequest) {
  const job: ActiveJob = { id: request.id, stdout: [], stderr: [] };
  active = job;
  let exitCode = 0;
  try {
    applyEnvironment(request.env);
    process.exitCode = undefined;
    await engineMain(request.argv);
    exitCode = Number(process.exitCode ?? 0) || 0;
  } catch (error) {
    job.stderr.push(`ERROR: ${errorMessage(error)}\n`);
    exitCode = 1;
  } finally {
    process.exitCode = undefined;
    active = undefined;
  }
  respond(job, exitCode);
}

async function serve(): Promise<void> {
  const { main: engineMain } = await import("./solana_flash_arb.js");
  writeProtocol(`${RESPONSE_PREFIX}${JSON.stringify({ ready: true, pid: process.pid })}\n`);

  const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  // The parent sends one request at a time; the loop keeps it strictly serial.
  for await (const line of lines) {
    if (!line.trim()) continue;
    let request: WorkerRequest;
    try {
      request = JSON.parse(line) as WorkerRequest;
    } catch {
      continue;
    }
    await runJob(engineMain, request);
  }
  process.exit(0);
}

serve().catch((error: unknown) => {
  // Startup failed before the ready line; the parent falls back to one-shot.
  writeDiagnostic(`Solana worker startup failed: ${errorMessage(error)}\n`);
  process.exit(1);
});
