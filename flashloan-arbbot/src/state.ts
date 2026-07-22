import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import path from "node:path";
import type { BotState, PendingSubmission } from "./types.js";

const INITIAL_STATE: BotState = {
  status: "idle",
  updatedAt: new Date(0).toISOString(),
};

export class StateStore {
  private state: BotState = { ...INITIAL_STATE };

  constructor(private readonly file: string) {}

  async load(): Promise<BotState> {
    try {
      this.state = JSON.parse(await readFile(this.file, "utf8")) as BotState;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
      this.state = { ...INITIAL_STATE };
    }
    return this.snapshot();
  }

  snapshot(): BotState {
    return JSON.parse(JSON.stringify(this.state)) as BotState;
  }

  async update(patch: Partial<BotState>): Promise<void> {
    this.state = {
      ...this.state,
      ...patch,
      updatedAt: new Date().toISOString(),
    };
    await this.persist();
  }

  async setPending(pending: PendingSubmission): Promise<void> {
    await this.update({ status: "pending", pending, message: `Pending ${pending.signature}` });
  }

  async clearPending(lastConfirmedSignature?: string): Promise<void> {
    this.state = {
      ...this.state,
      status: "idle",
      updatedAt: new Date().toISOString(),
      message: undefined,
      pending: undefined,
      ...(lastConfirmedSignature ? { lastConfirmedSignature } : {}),
    };
    await this.persist();
  }

  private async persist(): Promise<void> {
    await mkdir(path.dirname(this.file), { recursive: true });
    const temporary = `${this.file}.tmp`;
    await writeFile(temporary, `${JSON.stringify(this.state, null, 2)}\n`, "utf8");
    await rename(temporary, this.file);
  }
}
