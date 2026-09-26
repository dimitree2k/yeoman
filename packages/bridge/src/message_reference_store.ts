import { createHash, randomUUID } from 'node:crypto';
import {
  chmod,
  lstat,
  mkdir,
  open as openFile,
  readdir,
  readFile,
  rename,
  unlink,
  writeFile,
} from 'node:fs/promises';
import type { Stats } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

import { proto } from '@whiskeysockets/baileys/WAProto/index.js';

const OWNER_DIR_MODE = 0o700;
const OWNER_FILE_MODE = 0o600;
const DEFAULT_RETENTION_MS = 7 * 24 * 60 * 60 * 1000;
const DEFAULT_MAX_ENTRIES = 20_000;
const DEFAULT_MAX_RECORD_BYTES = 256 * 1024;
const RECORD_FILE_RE = /^[0-9a-f]{64}\.json$/;

export function defaultMessageReferenceDir(): string {
  // The override keeps a caller without an explicit `messageReferenceDir` - a test most
  // of all - out of the live runtime store the real bridge writes to. An empty value
  // means "unset", never "the working directory".
  const override = (process.env.BRIDGE_MESSAGE_REFERENCE_DIR || '').trim();
  if (override) return override;
  return join(homedir(), '.yeoman', 'data', 'bridge', 'whatsapp-message-references');
}

type StoredReference = {
  chatJid: string;
  messageId: string;
  storedAtMs: number;
  expiresAtMs: number;
  encoded: string;
};

type ReferenceEntry = {
  path: string;
  record: StoredReference;
};

export type MessageReferenceStoreOptions = {
  retentionMs?: number;
  maxEntries?: number;
  maxRecordBytes?: number;
};

function ownerId(): number | undefined {
  return typeof process.getuid === 'function' ? process.getuid() : undefined;
}

async function enforceOwnerOnly(path: string, stats: Stats, mode: number): Promise<void> {
  const uid = ownerId();
  if (uid !== undefined && stats.uid !== uid) throw new Error('Message reference owner mismatch');
  if ((stats.mode & 0o777) !== mode) {
    await chmod(path, mode);
    const checked = await lstat(path);
    if ((checked.mode & 0o777) !== mode) throw new Error('Message reference unsafe permissions');
  }
}

function exactKey(chatJid: string, messageId: string): string | undefined {
  if (!chatJid.trim() || !messageId.trim()) return undefined;
  return `${chatJid}\u0000${messageId}`;
}

function fileNameFor(key: string): string {
  return `${createHash('sha256').update(key, 'utf8').digest('hex')}.json`;
}

function isStoredReference(value: unknown): value is StoredReference {
  if (!value || typeof value !== 'object') return false;
  const record = value as Partial<StoredReference>;
  return typeof record.chatJid === 'string'
    && Boolean(record.chatJid.trim())
    && typeof record.messageId === 'string'
    && Boolean(record.messageId.trim())
    && typeof record.storedAtMs === 'number'
    && Number.isFinite(record.storedAtMs)
    && typeof record.expiresAtMs === 'number'
    && Number.isFinite(record.expiresAtMs)
    && record.expiresAtMs >= record.storedAtMs
    && typeof record.encoded === 'string'
    && Boolean(record.encoded);
}

export class MessageReferenceStore {
  private readonly entries = new Map<string, ReferenceEntry>();
  private readonly retentionMs: number;
  private readonly maxEntries: number;
  private readonly maxRecordBytes: number;
  private opened = false;
  private opening: Promise<void> | undefined;

  constructor(
    private readonly directory: string,
    options: MessageReferenceStoreOptions = {},
  ) {
    this.retentionMs = options.retentionMs ?? DEFAULT_RETENTION_MS;
    this.maxEntries = options.maxEntries ?? DEFAULT_MAX_ENTRIES;
    this.maxRecordBytes = options.maxRecordBytes ?? DEFAULT_MAX_RECORD_BYTES;
    if (!Number.isFinite(this.retentionMs) || this.retentionMs <= 0) throw new Error('Invalid message reference retention');
    if (!Number.isSafeInteger(this.maxEntries) || this.maxEntries <= 0) throw new Error('Invalid message reference entry limit');
    if (!Number.isSafeInteger(this.maxRecordBytes) || this.maxRecordBytes <= 0) throw new Error('Invalid message reference size limit');
  }

  async open(): Promise<void> {
    if (this.opened) return;
    if (!this.opening) this.opening = this.load();
    await this.opening;
  }

  async put(chatJid: string, messageId: string, message: unknown, nowMs = Date.now()): Promise<boolean> {
    await this.open();
    const key = exactKey(chatJid, messageId);
    if (!key) return false;

    let encoded: string;
    try {
      const value = proto.WebMessageInfo.fromObject(message as Record<string, unknown>);
      encoded = Buffer.from(proto.WebMessageInfo.encode(value).finish()).toString('base64');
    } catch {
      return false;
    }
    if (Buffer.byteLength(encoded, 'utf8') > this.maxRecordBytes) return false;

    await this.maintain(nowMs);
    const path = join(this.directory, fileNameFor(key));
    const record: StoredReference = {
      chatJid,
      messageId,
      storedAtMs: nowMs,
      expiresAtMs: nowMs + this.retentionMs,
      encoded,
    };
    const serialized = JSON.stringify(record);
    if (Buffer.byteLength(serialized, 'utf8') > this.maxRecordBytes + 1024) return false;

    const temporaryPath = join(this.directory, `.${fileNameFor(key)}.${randomUUID()}.tmp`);
    await writeFile(temporaryPath, serialized, { encoding: 'utf8', mode: OWNER_FILE_MODE, flag: 'wx' });
    try {
      await chmod(temporaryPath, OWNER_FILE_MODE);
      await rename(temporaryPath, path);
    } catch (error) {
      await unlink(temporaryPath).catch(() => undefined);
      throw error;
    }

    this.entries.set(key, { path, record });
    await this.maintain(nowMs);
    return true;
  }

  async get(chatJid: string, messageId: string, nowMs = Date.now()): Promise<unknown | undefined> {
    await this.open();
    await this.maintain(nowMs);
    const key = exactKey(chatJid, messageId);
    const entry = key ? this.entries.get(key) : undefined;
    if (!entry) return undefined;
    try {
      return proto.WebMessageInfo.decode(Buffer.from(entry.record.encoded, 'base64'));
    } catch {
      await this.remove(key!);
      return undefined;
    }
  }

  async has(chatJid: string, messageId: string, nowMs = Date.now()): Promise<boolean> {
    await this.open();
    await this.maintain(nowMs);
    const key = exactKey(chatJid, messageId);
    return key !== undefined && this.entries.has(key);
  }

  private async load(): Promise<void> {
    let stats: Stats;
    try {
      stats = await lstat(this.directory);
    } catch (error: unknown) {
      if ((error as { code?: string }).code !== 'ENOENT') throw error;
      await mkdir(this.directory, { recursive: true, mode: OWNER_DIR_MODE });
      stats = await lstat(this.directory);
    }
    if (!stats.isDirectory()) throw new Error('Message reference path is not a directory');
    await enforceOwnerOnly(this.directory, stats, OWNER_DIR_MODE);

    const nowMs = Date.now();
    for (const file of await readdir(this.directory, { withFileTypes: true })) {
      if (!RECORD_FILE_RE.test(file.name)) continue;
      const path = join(this.directory, file.name);
      const fileStats = await lstat(path);
      if (!fileStats.isFile()) throw new Error('Message reference candidate is not a regular file');
      await enforceOwnerOnly(path, fileStats, OWNER_FILE_MODE);

      let value: unknown;
      try {
        value = JSON.parse(await readFile(path, 'utf8'));
      } catch {
        await unlink(path);
        continue;
      }
      if (!isStoredReference(value)) {
        await unlink(path);
        continue;
      }
      const key = exactKey(value.chatJid, value.messageId);
      if (!key || file.name !== fileNameFor(key) || Buffer.byteLength(value.encoded, 'utf8') > this.maxRecordBytes) {
        await unlink(path);
        continue;
      }
      if (value.expiresAtMs <= nowMs) {
        await unlink(path);
        continue;
      }
      this.entries.set(key, { path, record: value });
    }
    await this.maintain(nowMs);
    this.opened = true;
  }

  private async maintain(nowMs: number): Promise<void> {
    for (const [key, entry] of this.entries) {
      if (entry.record.expiresAtMs <= nowMs) await this.remove(key);
    }
    while (this.entries.size > this.maxEntries) {
      const oldest = [...this.entries.entries()].sort(([, left], [, right]) => left.record.storedAtMs - right.record.storedAtMs)[0];
      if (!oldest) break;
      await this.remove(oldest[0]);
    }
  }

  private async remove(key: string): Promise<void> {
    const entry = this.entries.get(key);
    if (!entry) return;
    await unlink(entry.path).catch((error: unknown) => {
      if ((error as { code?: string }).code !== 'ENOENT') throw error;
    });
    this.entries.delete(key);
  }
}
