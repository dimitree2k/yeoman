import { createHash, randomUUID } from 'node:crypto';
import {
  mkdir,
  open as openFile,
  readdir,
  readFile,
  rename,
  unlink,
} from 'node:fs/promises';
import type { Dirent } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

import type { BridgeEventEnvelope } from './protocol.js';

export type ReplayableBridgeEvent = BridgeEventEnvelope & {
  eventId: string;
  eventKey: string;
  observedAt: number;
};

type OutboxEntry = {
  sequence: number;
  fileName: string;
  event: ReplayableBridgeEvent;
};

const OUTBOX_FILE_RE = /^(\d+)-.+\.json$/;

export function defaultBridgeOutboxDir(): string {
  return join(homedir(), '.yeoman', 'data', 'bridge', 'whatsapp-outbox');
}

function eventKeyFor(event: BridgeEventEnvelope): string {
  return createHash('sha256')
    .update(JSON.stringify({ type: event.type, accountId: event.accountId, payload: event.payload }))
    .digest('hex');
}

function asReplayableEvent(event: BridgeEventEnvelope): ReplayableBridgeEvent {
  const eventId = typeof event.eventId === 'string' && event.eventId.trim() ? event.eventId : randomUUID();
  const eventKey = typeof event.eventKey === 'string' && event.eventKey.trim()
    ? event.eventKey
    : eventKeyFor(event);
  const observedAt = typeof event.observedAt === 'number' && Number.isFinite(event.observedAt)
    ? event.observedAt
    : typeof event.ts === 'number' && Number.isFinite(event.ts)
      ? event.ts
      : Date.now();
  return { ...event, eventId, eventKey, observedAt };
}

function fileNameFor(sequence: number, event: ReplayableBridgeEvent): string {
  return `${String(sequence).padStart(20, '0')}-${encodeURIComponent(event.eventId)}-${encodeURIComponent(event.eventKey)}.json`;
}

async function syncDirectory(path: string): Promise<void> {
  const handle = await openFile(path, 'r');
  try {
    await handle.sync();
  } finally {
    await handle.close();
  }
}

export class BridgeOutbox {
  private readonly entries = new Map<string, OutboxEntry>();
  private nextSequence = 1;
  private opened = false;
  private opening: Promise<void> | null = null;
  private writeTail: Promise<void> = Promise.resolve();

  constructor(private readonly directory: string = defaultBridgeOutboxDir()) {}

  async open(): Promise<void> {
    if (this.opened) return;
    if (!this.opening) this.opening = this.load();
    await this.opening;
  }

  private async load(): Promise<void> {
    await mkdir(this.directory, { recursive: true, mode: 0o700 });
    const files = await readdir(this.directory, { withFileTypes: true });
    const loaded: OutboxEntry[] = [];
    for (const file of files) {
      if (!this.isPendingFile(file)) continue;
      const match = OUTBOX_FILE_RE.exec(file.name);
      if (!match) continue;
      const sequence = Number(match[1]);
      if (!Number.isSafeInteger(sequence) || sequence < 1) continue;
      const raw = await readFile(join(this.directory, file.name), 'utf8');
      let parsed: unknown;
      try {
        parsed = JSON.parse(raw);
      } catch {
        throw new Error(`Invalid bridge outbox entry ${file.name}`);
      }
      if (!this.isReplayableEvent(parsed)) {
        throw new Error(`Invalid bridge outbox entry ${file.name}`);
      }
      loaded.push({ sequence, fileName: file.name, event: parsed });
    }

    loaded.sort((a, b) => a.sequence - b.sequence);
    for (const entry of loaded) {
      if (this.entries.has(entry.event.eventId)) {
        throw new Error('Duplicate bridge outbox event');
      }
      this.entries.set(entry.event.eventId, entry);
      this.nextSequence = Math.max(this.nextSequence, entry.sequence + 1);
    }
    this.opened = true;
  }

  private isPendingFile(file: Dirent): boolean {
    return file.isFile() && file.name.endsWith('.json') && OUTBOX_FILE_RE.test(file.name);
  }

  private isReplayableEvent(value: unknown): value is ReplayableBridgeEvent {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
    const event = value as Record<string, unknown>;
    return (
      typeof event.eventId === 'string' &&
      event.eventId.length > 0 &&
      typeof event.eventKey === 'string' &&
      event.eventKey.length > 0 &&
      typeof event.observedAt === 'number' &&
      Number.isFinite(event.observedAt) &&
      typeof event.type === 'string' &&
      typeof event.accountId === 'string' &&
      Boolean(event.payload) &&
      typeof event.payload === 'object' &&
      !Array.isArray(event.payload)
    );
  }

  private enqueue<T>(operation: () => Promise<T>): Promise<T> {
    const result = this.writeTail.then(operation, operation);
    this.writeTail = result.then(() => undefined, () => undefined);
    return result;
  }

  async append(event: BridgeEventEnvelope): Promise<ReplayableBridgeEvent> {
    await this.open();
    const replayable = asReplayableEvent(event);
    return this.enqueue(async () => {
      const existing = this.entries.get(replayable.eventId);
      if (existing) {
        if (JSON.stringify(existing.event) !== JSON.stringify(replayable)) {
          throw new Error('Conflicting bridge outbox event');
        }
        return existing.event;
      }

      const sequence = this.nextSequence;
      const fileName = fileNameFor(sequence, replayable);
      const finalPath = join(this.directory, fileName);
      const temporaryPath = join(this.directory, `.${fileName}.${randomUUID()}.tmp`);
      const handle = await openFile(temporaryPath, 'wx', 0o600);
      try {
        await handle.writeFile(JSON.stringify(replayable), 'utf8');
        await handle.sync();
      } finally {
        await handle.close();
      }
      try {
        await rename(temporaryPath, finalPath);
        await syncDirectory(this.directory);
      } catch (error) {
        await unlink(temporaryPath).catch(() => undefined);
        throw error;
      }

      this.nextSequence += 1;
      this.entries.set(replayable.eventId, { sequence, fileName, event: replayable });
      return replayable;
    });
  }

  async pending(): Promise<ReplayableBridgeEvent[]> {
    await this.open();
    await this.writeTail;
    return Array.from(this.entries.values())
      .sort((a, b) => a.sequence - b.sequence)
      .map(({ event }) => event);
  }

  async ack(eventId: string): Promise<boolean> {
    await this.open();
    return this.enqueue(async () => {
      const entry = this.entries.get(eventId);
      if (!entry) return false;
      await unlink(join(this.directory, entry.fileName)).catch((error: unknown) => {
        const code = error && typeof error === 'object' ? (error as { code?: string }).code : undefined;
        if (code !== 'ENOENT') throw error;
      });
      await syncDirectory(this.directory);
      this.entries.delete(eventId);
      return true;
    });
  }

  diagnostics(): { pending: number; nextSequence: number } {
    return { pending: this.entries.size, nextSequence: this.nextSequence };
  }
}
