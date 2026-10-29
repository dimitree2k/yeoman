import { createHash, createHmac } from 'node:crypto';
import { chmod, mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';

import { decryptPollVote } from '@whiskeysockets/baileys';

import { yeomanHome } from './runtime_paths.js';

const OWNER_DIR_MODE = 0o700;
const OWNER_FILE_MODE = 0o600;
/** Longer than the gateway keeps a poll open, so its results stay readable until then. */
export const POLL_REFERENCE_TTL_MS = 48 * 60 * 60 * 1000;
const MAX_POLLS = 200;
const MAX_VOTERS_PER_POLL = 2048;

export function defaultPollReferenceFile(): string {
  // The override keeps tests out of the live runtime store; empty means "unset".
  const override = (process.env.BRIDGE_POLL_REFERENCE_FILE || '').trim();
  if (override) return override;
  return join(yeomanHome(), 'data', 'ops', 'bridge-poll-references.json');
}

/**
 * One poll the bridge sent. Votes are keyed by an HMAC of the voter under the poll's own
 * secret, so the file never holds a voter JID and the tags die with the poll.
 */
type StoredPoll = {
  chatJid: string;
  pollId: string;
  secret: string;
  options: string[];
  createdAtMs: number;
  expiresAtMs: number;
  updatedAtMs: number;
  votes: Record<string, { selected: string[]; atMs: number }>;
};

export interface PollResults {
  options: { name: string; count: number }[];
  total_votes: number;
  updated_at_ms: number;
}

export interface PollVoteInput {
  chatJid: string;
  pollId: string;
  encPayload: Uint8Array;
  encIv: Uint8Array;
  /** Every JID form the voter may have signed with (LID and phone number). */
  voterJids: string[];
  /** Every JID form this account may have created the poll as. */
  creatorJids: string[];
  senderTimestampMs: number;
}

function key(chatJid: string, pollId: string): string {
  return `${chatJid}\u0000${pollId}`;
}

function optionHash(name: string): string {
  return createHash('sha256').update(Buffer.from(name)).digest('hex');
}

export class PollReferenceStore {
  private polls = new Map<string, StoredPoll>();
  private loaded = false;
  private writes: Promise<void> = Promise.resolve();

  constructor(private readonly path: string = defaultPollReferenceFile()) {}

  async open(nowMs = Date.now()): Promise<void> {
    if (this.loaded) return;
    this.loaded = true;
    try {
      const raw = JSON.parse(await readFile(this.path, 'utf8'));
      for (const poll of Array.isArray(raw?.polls) ? raw.polls : []) {
        if (isStoredPoll(poll) && poll.expiresAtMs > nowMs) {
          this.polls.set(key(poll.chatJid, poll.pollId), poll);
        }
      }
    } catch (error: any) {
      if (error?.code !== 'ENOENT') this.polls.clear();
    }
  }

  async remember(input: {
    chatJid: string; pollId: string; secret: Uint8Array; options: string[];
  }, nowMs = Date.now()): Promise<void> {
    await this.open(nowMs);
    if (!input.chatJid || !input.pollId || input.secret.length === 0) return;
    this.polls.set(key(input.chatJid, input.pollId), {
      chatJid: input.chatJid,
      pollId: input.pollId,
      secret: Buffer.from(input.secret).toString('base64'),
      options: [...input.options],
      createdAtMs: nowMs,
      expiresAtMs: nowMs + POLL_REFERENCE_TTL_MS,
      updatedAtMs: nowMs,
      votes: {},
    });
    await this.persist(nowMs);
  }

  /** Whether a vote belongs to one of this bridge's polls in that chat. */
  async knows(chatJid: string, pollId: string, nowMs = Date.now()): Promise<boolean> {
    return (await this.live(chatJid, pollId, nowMs)) !== undefined;
  }

  /**
   * Decrypt and count one vote. Returns false when the poll is unknown or no JID pair
   * decrypts it; the voter JID is used for decryption only and never stored.
   */
  async recordVote(vote: PollVoteInput, nowMs = Date.now()): Promise<boolean> {
    const poll = await this.live(vote.chatJid, vote.pollId, nowMs);
    if (!poll) return false;
    const pollEncKey = Buffer.from(poll.secret, 'base64');
    for (const creator of unique(vote.creatorJids)) {
      for (const voter of unique(vote.voterJids)) {
        let selected: string[];
        try {
          const decoded = decryptPollVote(
            { encPayload: vote.encPayload, encIv: vote.encIv },
            { pollCreatorJid: creator, pollMsgId: poll.pollId, pollEncKey, voterJid: voter },
          );
          selected = (decoded.selectedOptions || []).map((x) => Buffer.from(x).toString('hex'));
        } catch {
          continue;
        }
        const tag = createHmac('sha256', pollEncKey).update(voter).digest('hex');
        const previous = poll.votes[tag];
        // A later vote replaces an earlier one; delivery can reorder them.
        if (previous && previous.atMs > vote.senderTimestampMs) return true;
        if (!previous && Object.keys(poll.votes).length >= MAX_VOTERS_PER_POLL) return true;
        if (selected.length) poll.votes[tag] = { selected, atMs: vote.senderTimestampMs };
        else delete poll.votes[tag];
        poll.updatedAtMs = nowMs;
        await this.persist(nowMs);
        return true;
      }
    }
    return false;
  }

  /** Counts only, or undefined when this chat has no such poll from this bridge. */
  async results(chatJid: string, pollId: string, nowMs = Date.now()): Promise<PollResults | undefined> {
    const poll = await this.live(chatJid, pollId, nowMs);
    if (!poll) return undefined;
    const hashes = poll.options.map(optionHash);
    const counts = hashes.map(() => 0);
    const voters = Object.values(poll.votes);
    for (const { selected } of voters) {
      for (const choice of new Set(selected)) {
        const index = hashes.indexOf(choice);
        if (index >= 0) counts[index] += 1;
      }
    }
    return {
      options: poll.options.map((name, index) => ({ name, count: counts[index] })),
      total_votes: voters.length,
      updated_at_ms: poll.updatedAtMs,
    };
  }

  async forget(chatJid: string, pollId: string, nowMs = Date.now()): Promise<void> {
    await this.open(nowMs);
    if (this.polls.delete(key(chatJid, pollId))) await this.persist(nowMs);
  }

  private async live(chatJid: string, pollId: string, nowMs: number): Promise<StoredPoll | undefined> {
    await this.open(nowMs);
    const poll = this.polls.get(key(chatJid, pollId));
    if (!poll) return undefined;
    if (poll.expiresAtMs <= nowMs) {
      this.polls.delete(key(chatJid, pollId));
      await this.persist(nowMs);
      return undefined;
    }
    return poll;
  }

  private persist(nowMs: number): Promise<void> {
    for (const [id, poll] of this.polls) {
      if (poll.expiresAtMs <= nowMs) this.polls.delete(id);
    }
    const kept = [...this.polls.values()].sort((a, b) => b.createdAtMs - a.createdAtMs).slice(0, MAX_POLLS);
    this.polls = new Map(kept.map((poll) => [key(poll.chatJid, poll.pollId), poll]));
    const body = JSON.stringify({ polls: kept });
    const next = this.writes.then(() => this.write(body));
    this.writes = next.catch(() => undefined);
    return next;
  }

  private async write(body: string): Promise<void> {
    await mkdir(dirname(this.path), { recursive: true, mode: OWNER_DIR_MODE });
    const tmp = `${this.path}.tmp`;
    await writeFile(tmp, body, { mode: OWNER_FILE_MODE });
    await chmod(tmp, OWNER_FILE_MODE);
    await rename(tmp, this.path);
  }
}

function unique(values: string[]): string[] {
  return [...new Set(values.map((x) => String(x || '').trim()).filter(Boolean))];
}

function isStoredPoll(value: any): value is StoredPoll {
  return Boolean(value) && typeof value.chatJid === 'string' && typeof value.pollId === 'string' &&
    typeof value.secret === 'string' && Array.isArray(value.options) &&
    value.options.every((x: unknown) => typeof x === 'string') &&
    Number.isFinite(value.expiresAtMs) && Number.isFinite(value.updatedAtMs) &&
    Number.isFinite(value.createdAtMs) && value.votes !== null && typeof value.votes === 'object';
}
