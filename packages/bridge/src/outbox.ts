import { createHash, randomUUID } from 'node:crypto';
import {
  chmod,
  mkdir,
  open as openFile,
  readdir,
  readFile,
  rename,
  unlink,
  lstat,
} from 'node:fs/promises';
import type { Stats } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

import {
  PROTOCOL_VERSION,
  type BridgeEventEnvelope,
  type BridgeEventType,
} from './protocol.js';

export type ReplayableEventType = Extract<
  BridgeEventType,
  'message' | 'edit' | 'delete' | 'reaction' | 'receipt'
>;

export type ReplayableBridgeEvent = BridgeEventEnvelope & {
  type: ReplayableEventType;
  eventId: string;
  eventKey: string;
  observedAt: number;
};

type OutboxEntry = {
  sequence: number;
  fileName: string;
  event: ReplayableBridgeEvent;
};

export type OutboxDiagnostics = {
  pending: number;
  nextSequence: number;
  quarantined: number;
};

const OUTBOX_FILE_RE = /^(\d+)-.+\.json$/;
const STAGED_FILE_RE = /^\.((\d+)-.+\.json)\.[0-9a-f-]+\.tmp$/;
const QUARANTINE_DIR = 'quarantine';
const OWNER_DIR_MODE = 0o700;
const OWNER_FILE_MODE = 0o600;

export function defaultBridgeOutboxDir(): string {
  return join(homedir(), '.yeoman', 'data', 'bridge', 'whatsapp-outbox');
}

export function isReplayableEventType(type: string): type is ReplayableEventType {
  return type === 'message' || type === 'edit' || type === 'delete' || type === 'reaction' || type === 'receipt';
}

function eventKeyFor(event: BridgeEventEnvelope): string {
  return createHash('sha256')
    .update(JSON.stringify({ type: event.type, accountId: event.accountId, payload: event.payload }))
    .digest('hex');
}

function asReplayableEvent(event: BridgeEventEnvelope): ReplayableBridgeEvent {
  if (!isReplayableEventType(event.type)) {
    throw new Error(`Invalid replayable event type: ${event.type}`);
  }
  const type = event.type;
  const eventId = typeof event.eventId === 'string' && event.eventId.trim() ? event.eventId : randomUUID();
  const eventKey = typeof event.eventKey === 'string' && event.eventKey.trim()
    ? event.eventKey
    : eventKeyFor(event);
  const observedAt = typeof event.observedAt === 'number' && Number.isFinite(event.observedAt)
    ? event.observedAt
    : typeof event.ts === 'number' && Number.isFinite(event.ts)
      ? event.ts
      : Date.now();
  return { ...event, type, eventId, eventKey, observedAt };
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

function ownerId(): number | undefined {
  return typeof process.getuid === 'function' ? process.getuid() : undefined;
}

async function enforceOwnerOnly(path: string, stats: Stats, mode: number): Promise<Stats> {
  const uid = ownerId();
  if (uid !== undefined && stats.uid !== uid) throw new Error('Bridge outbox owner mismatch');
  if ((stats.mode & 0o777) !== mode) {
    await chmod(path, mode);
    const checked = await lstat(path);
    if ((checked.mode & 0o777) !== mode) throw new Error('Bridge outbox unsafe permissions');
    return checked;
  }
  return stats;
}

export class BridgeOutbox {
  private readonly entries = new Map<string, OutboxEntry>();
  private nextSequence = 1;
  private quarantined = 0;
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
    let directoryStats: Stats;
    try {
      directoryStats = await lstat(this.directory);
    } catch (error: unknown) {
      if ((error as { code?: string }).code !== 'ENOENT') throw error;
      await mkdir(this.directory, { recursive: true, mode: OWNER_DIR_MODE });
      directoryStats = await lstat(this.directory);
    }
    if (!directoryStats.isDirectory()) throw new Error('Bridge outbox path is not a directory');
    await enforceOwnerOnly(this.directory, directoryStats, OWNER_DIR_MODE);

    const quarantinePath = join(this.directory, QUARANTINE_DIR);
    let quarantineStats: Stats;
    try {
      quarantineStats = await lstat(quarantinePath);
    } catch (error: unknown) {
      if ((error as { code?: string }).code !== 'ENOENT') throw error;
      await mkdir(quarantinePath, { mode: OWNER_DIR_MODE });
      quarantineStats = await lstat(quarantinePath);
    }
    if (!quarantineStats.isDirectory()) throw new Error('Bridge outbox quarantine is not a directory');
    await enforceOwnerOnly(quarantinePath, quarantineStats, OWNER_DIR_MODE);
    for (const quarantineEntry of await readdir(quarantinePath, { withFileTypes: true })) {
      const path = join(quarantinePath, quarantineEntry.name);
      const stats = await lstat(path);
      if (!stats.isFile()) throw new Error('Bridge outbox quarantine candidate is not a regular file');
      await enforceOwnerOnly(path, stats, OWNER_FILE_MODE);
    }

    const files = (await readdir(this.directory, { withFileTypes: true }))
      .sort((a, b) => {
        const aStaged = a.name.startsWith('.') ? 1 : 0;
        const bStaged = b.name.startsWith('.') ? 1 : 0;
        return aStaged - bStaged || a.name.localeCompare(b.name);
      });
    const loaded: OutboxEntry[] = [];
    const sequences = new Set<number>();
    const eventIds = new Set<string>();
    for (const file of files) {
      if (file.name === QUARANTINE_DIR) continue;
      const path = join(this.directory, file.name);
      const stats = await lstat(path);
      if (!stats.isFile()) throw new Error('Bridge outbox candidate is not a regular file');
      await enforceOwnerOnly(path, stats, OWNER_FILE_MODE);

      const match = OUTBOX_FILE_RE.exec(file.name);
      const stagedMatch = STAGED_FILE_RE.exec(file.name);
      if (!match && !stagedMatch) {
        await this.quarantine(path, 'malformed-filename');
        continue;
      }
      const sequence = Number(match?.[1] ?? stagedMatch?.[2]);
      if (!Number.isSafeInteger(sequence) || sequence < 1) {
        await this.quarantine(path, 'sequence-overflow');
        continue;
      }

      let parsed: unknown;
      try {
        parsed = JSON.parse(await readFile(path, 'utf8'));
      } catch {
        await this.quarantine(path, 'malformed-json');
        continue;
      }
      if (!this.isReplayableEvent(parsed)) {
        await this.quarantine(path, 'non-replayable');
        continue;
      }
      const expectedFileName = fileNameFor(sequence, parsed);
      if (match && expectedFileName !== file.name) {
        await this.quarantine(path, 'identity-mismatch');
        continue;
      }
      if (stagedMatch && expectedFileName !== stagedMatch[1]) {
        await this.quarantine(path, 'identity-mismatch');
        continue;
      }
      if (sequences.has(sequence)) {
        await this.quarantine(path, 'duplicate-sequence');
        continue;
      }
      if (eventIds.has(parsed.eventId)) {
        await this.quarantine(path, 'duplicate-event');
        continue;
      }
      sequences.add(sequence);
      eventIds.add(parsed.eventId);
      loaded.push({ sequence, fileName: file.name, event: parsed });
    }

    loaded.sort((a, b) => a.sequence - b.sequence);
    for (const entry of loaded) {
      this.entries.set(entry.event.eventId, entry);
      this.nextSequence = Math.max(this.nextSequence, entry.sequence + 1);
    }
    this.opened = true;
  }

  private async quarantine(path: string, reason: string): Promise<void> {
    const target = join(this.directory, QUARANTINE_DIR, `${Date.now()}-${randomUUID()}-${reason}.json`);
    try {
      await rename(path, target);
      await chmod(target, OWNER_FILE_MODE);
      await syncDirectory(join(this.directory, QUARANTINE_DIR));
      await syncDirectory(this.directory);
      this.quarantined += 1;
    } catch {
      throw new Error('Bridge outbox quarantine failed');
    }
  }

  private isReplayableEvent(value: unknown): value is ReplayableBridgeEvent {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
    const event = value as Record<string, unknown>;
    return (
      event.version === PROTOCOL_VERSION &&
      typeof event.type === 'string' &&
      isReplayableEventType(event.type) &&
      typeof event.ts === 'number' &&
      Number.isFinite(event.ts) &&
      typeof event.accountId === 'string' &&
      typeof event.eventId === 'string' &&
      event.eventId.length > 0 &&
      typeof event.eventKey === 'string' &&
      event.eventKey.length > 0 &&
      typeof event.observedAt === 'number' &&
      Number.isFinite(event.observedAt) &&
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

  private async rememberStaged(
    sequence: number,
    fileName: string,
    expected: ReplayableBridgeEvent,
  ): Promise<void> {
    const path = join(this.directory, fileName);
    const stats = await lstat(path);
    if (!stats.isFile()) throw new Error('Bridge outbox staged record is not a regular file');
    await enforceOwnerOnly(path, stats, OWNER_FILE_MODE);
    const parsed: unknown = JSON.parse(await readFile(path, 'utf8'));
    if (!this.isReplayableEvent(parsed) || JSON.stringify(parsed) !== JSON.stringify(expected)) {
      throw new Error('Bridge outbox staged record mismatch');
    }
    const existing = this.entries.get(expected.eventId);
    if (existing) {
      if (JSON.stringify(existing.event) !== JSON.stringify(expected)) {
        throw new Error('Conflicting bridge outbox event');
      }
      return;
    }
    this.entries.set(expected.eventId, { sequence, fileName, event: expected });
    this.nextSequence = Math.max(this.nextSequence, sequence + 1);
  }

  async append(event: BridgeEventEnvelope): Promise<ReplayableBridgeEvent> {
    const replayable = asReplayableEvent(event);
    await this.open();
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
      const temporaryFileName = temporaryPath.slice(this.directory.length + 1);
      let handle: Awaited<ReturnType<typeof openFile>> | undefined;
      let staged = false;
      try {
        handle = await openFile(temporaryPath, 'wx', OWNER_FILE_MODE);
        await handle.writeFile(JSON.stringify(replayable), 'utf8');
        await handle.sync();
        staged = true;
        await handle.close();
        handle = undefined;
        // Make the retry filename durable before attempting the canonical rename.
        await syncDirectory(this.directory);
      } catch (error) {
        await handle?.close().catch(() => undefined);
        if (!staged) await unlink(temporaryPath).catch(() => undefined);
        else await this.rememberStaged(sequence, temporaryFileName, replayable).catch(() => undefined);
        throw error;
      }

      let renamed = false;
      try {
        await rename(temporaryPath, finalPath);
        renamed = true;
        await syncDirectory(this.directory);
      } catch (error) {
        if (renamed) await this.reconcilePersisted(sequence, fileName, replayable);
        else await this.rememberStaged(sequence, temporaryFileName, replayable).catch(() => undefined);
        throw error;
      }

      this.nextSequence += 1;
      this.entries.set(replayable.eventId, { sequence, fileName, event: replayable });
      return replayable;
    });
  }

  private async reconcilePersisted(
    sequence: number,
    fileName: string,
    expected: ReplayableBridgeEvent,
  ): Promise<void> {
    try {
      const path = join(this.directory, fileName);
      const stats = await lstat(path);
      if (!stats.isFile()) throw new Error('not regular');
      await enforceOwnerOnly(path, stats, OWNER_FILE_MODE);
      const parsed: unknown = JSON.parse(await readFile(path, 'utf8'));
      if (!this.isReplayableEvent(parsed) || JSON.stringify(parsed) !== JSON.stringify(expected)) {
        throw new Error('mismatch');
      }
      this.entries.set(expected.eventId, { sequence, fileName, event: expected });
      this.nextSequence = Math.max(this.nextSequence, sequence + 1);
    } catch {
      throw new Error('Bridge outbox write outcome could not be reconciled');
    }
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

  async flush(): Promise<void> {
    await this.open();
    await this.writeTail;
  }

  diagnostics(): OutboxDiagnostics {
    return {
      pending: this.entries.size,
      nextSequence: this.nextSequence,
      quarantined: this.quarantined,
    };
  }
}
