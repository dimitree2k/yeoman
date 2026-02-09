/* eslint-disable @typescript-eslint/no-explicit-any */
import { createHash } from 'crypto';
import { promises as fs } from 'fs';

import makeWASocket, {
  DisconnectReason,
  fetchLatestBaileysVersion,
  makeCacheableSignalKeyStore,
  useMultiFileAuthState,
} from '@whiskeysockets/baileys';
import { Boom } from '@hapi/boom';
import pino from 'pino';
import qrcode from 'qrcode-terminal';

const VERSION = '0.2.0';
const INBOUND_DEDUPE_TTL_MS = 20 * 60_000;
const INBOUND_DEDUPE_MAX = 5_000;
const INBOUND_DEDUPE_CLEANUP_INTERVAL_MS = 30_000;
const MAX_RECONNECT_ATTEMPTS = 30;
const MENTION_TOKEN_PATTERN = /@([0-9]{5,})/g;

export interface InboundMedia {
  kind: 'image' | 'video' | 'audio' | 'document' | 'sticker';
  mimeType?: string;
  fileName?: string;
}

export interface InboundMessageV2 {
  messageId: string;
  chatJid: string;
  participantJid: string;
  senderId: string;
  isGroup: boolean;
  text: string;
  timestamp: number;
  mentionedJids: string[];
  mentionedBot: boolean;
  replyToBot: boolean;
  replyToMessageId?: string;
  replyToParticipantJid?: string;
  replyToText?: string;
  media?: InboundMedia;
}

export interface WhatsAppHealth {
  connected: boolean;
  running: boolean;
  reconnectAttempts: number;
  lastDisconnectStatus?: number;
  lastError?: string;
  lastMessageAt?: number;
  droppedInboundDuplicates: number;
  dedupeCacheSize: number;
}

export interface SendMediaInput {
  to: string;
  mediaUrl?: string;
  mediaBase64?: string;
  mimeType?: string;
  fileName?: string;
  caption?: string;
}

export interface SendPollInput {
  to: string;
  question: string;
  options: string[];
  maxSelections?: number;
}

export interface ReactInput {
  chatJid: string;
  messageId: string;
  emoji: string;
  participantJid?: string;
  fromMe?: boolean;
}

export type PresenceState = 'available' | 'unavailable' | 'composing' | 'paused' | 'recording';

export interface PresenceUpdateInput {
  state: PresenceState;
  chatJid?: string;
}

export interface WhatsAppClientOptions {
  authDir: string;
  accountId?: string;
  onMessage: (msg: InboundMessageV2) => void;
  onQR: (qr: string) => void;
  onStatus: (status: string, detail?: Record<string, unknown>) => void;
  onError: (error: string) => void;
}

function nowMs(): number {
  return Date.now();
}

function normalizeJid(jidRaw: string): string {
  const trimmed = (jidRaw || '').trim();
  if (!trimmed) return '';
  const [leftRaw, right = ''] = trimmed.split('@', 2);
  const left = (leftRaw || '').split(':', 1)[0] || '';
  return right ? `${left}@${right}` : left;
}

function jidUserToken(jidRaw: string): string {
  const normalized = normalizeJid(jidRaw);
  return normalized.split('@', 1)[0] || '';
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function randomJitter(baseMs: number, jitterRatio: number): number {
  const delta = Math.max(0, Math.floor(baseMs * jitterRatio));
  if (delta === 0) return baseMs;
  const low = baseMs - delta;
  const high = baseMs + delta;
  return low + Math.floor(Math.random() * (high - low + 1));
}

function safeErrorMessage(err: unknown): string {
  if (err instanceof Error) return err.message;
  if (typeof err === 'string') return err;
  return String(err);
}

async function ensureAuthSecurity(authDir: string): Promise<void> {
  await fs.mkdir(authDir, { recursive: true, mode: 0o700 });
  const dirStat = await fs.stat(authDir);
  if ((dirStat.mode & 0o077) !== 0) {
    await fs.chmod(authDir, 0o700);
  }

  const entries = await fs.readdir(authDir).catch(() => [] as string[]);
  for (const name of entries) {
    if (!name.endsWith('.json')) continue;
    const path = `${authDir}/${name}`;
    try {
      const st = await fs.stat(path);
      if (!st.isFile()) continue;
      if ((st.mode & 0o077) !== 0) {
        await fs.chmod(path, 0o600);
      }
    } catch {
      // Ignore stat/chmod races.
    }
  }
}

function mediaKindFromMime(mimeType: string | undefined): InboundMedia['kind'] {
  const mime = (mimeType || '').toLowerCase();
  if (mime.startsWith('image/')) return 'image';
  if (mime.startsWith('video/')) return 'video';
  if (mime.startsWith('audio/')) return 'audio';
  return 'document';
}

function limitText(value: string, max = 10_000): string {
  const text = String(value || '');
  if (text.length <= max) return text;
  return text.slice(0, max);
}

async function loadMediaSource(input: SendMediaInput): Promise<{
  buffer: Buffer;
  mimeType: string;
  fileName?: string;
}> {
  if (input.mediaBase64) {
    const clean = input.mediaBase64.replace(/^data:[^;]+;base64,/, '').trim();
    const buffer = Buffer.from(clean, 'base64');
    if (buffer.length === 0) {
      throw new Error('mediaBase64 decoded to empty payload');
    }
    return {
      buffer,
      mimeType: input.mimeType || 'application/octet-stream',
      fileName: input.fileName,
    };
  }

  if (!input.mediaUrl) {
    throw new Error('Missing media source');
  }

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 20_000);
  try {
    const res = await fetch(input.mediaUrl, { signal: controller.signal });
    if (!res.ok) {
      throw new Error(`Media download failed with status ${res.status}`);
    }
    const arrayBuffer = await res.arrayBuffer();
    const buffer = Buffer.from(arrayBuffer);
    if (buffer.length === 0) {
      throw new Error('Downloaded media is empty');
    }
    return {
      buffer,
      mimeType: input.mimeType || res.headers.get('content-type') || 'application/octet-stream',
      fileName: input.fileName,
    };
  } finally {
    clearTimeout(timeout);
  }
}

export class WhatsAppClient {
  private readonly options: WhatsAppClientOptions;
  private sock: any = null;
  private running = false;
  private loopTask: Promise<void> | null = null;

  private connected = false;
  private reconnectAttempts = 0;
  private lastDisconnectStatus: number | undefined;
  private lastError: string | undefined;
  private lastMessageAt: number | undefined;
  private droppedInboundDuplicates = 0;

  private readonly recentInbound = new Map<string, number>();
  private nextInboundCleanupAt = 0;
  private latestQr: string | null = null;
  private latestQrAt = 0;

  private selfJids = new Set<string>();
  private selfTokens = new Set<string>();

  private qrWaiters = new Set<(value: string) => void>();
  private connectWaiters = new Set<(value: boolean) => void>();
  private baileysVersionCache: [number, number, number] | null = null;

  constructor(options: WhatsAppClientOptions) {
    this.options = options;
  }

  async start(): Promise<void> {
    if (this.running) return;
    this.running = true;
    this.loopTask = this.runLoop();
  }

  private async runLoop(): Promise<void> {
    const maxDelayMs = 30_000;
    const initialDelayMs = 1_000;
    while (this.running) {
      try {
        await this.connectOnce();
      } catch (err) {
        this.lastError = safeErrorMessage(err);
        this.options.onError(this.lastError);
      }

      if (!this.running) break;

      this.reconnectAttempts += 1;
      if (this.reconnectAttempts > MAX_RECONNECT_ATTEMPTS) {
        this.running = false;
        this.options.onStatus('reconnect_exhausted', {
          reconnectAttempts: this.reconnectAttempts,
          maxReconnectAttempts: MAX_RECONNECT_ATTEMPTS,
          lastDisconnectStatus: this.lastDisconnectStatus,
        });
        this.lastError = `Reconnect attempts exhausted (${MAX_RECONNECT_ATTEMPTS})`;
        this.options.onError(this.lastError);
        break;
      }
      this.options.onStatus('reconnecting', {
        reconnectAttempts: this.reconnectAttempts,
        lastDisconnectStatus: this.lastDisconnectStatus,
      });

      const backoff = Math.min(maxDelayMs, initialDelayMs * 2 ** Math.max(0, this.reconnectAttempts - 1));
      const delayMs = randomJitter(backoff, 0.25);
      await sleep(delayMs);
    }
  }

  private updateSelfIds(state: any): void {
    this.selfJids.clear();
    this.selfTokens.clear();
    const me = state?.creds?.me;
    for (const key of ['id', 'lid']) {
      const raw = typeof me?.[key] === 'string' ? me[key].trim() : '';
      if (!raw) continue;
      const normalized = normalizeJid(raw);
      if (normalized) this.selfJids.add(normalized);
      const token = jidUserToken(raw);
      if (token) this.selfTokens.add(token);
    }
  }

  private cleanupRecentInbound(): void {
    const now = nowMs();
    if (now < this.nextInboundCleanupAt && this.recentInbound.size <= INBOUND_DEDUPE_MAX) return;
    this.nextInboundCleanupAt = now + INBOUND_DEDUPE_CLEANUP_INTERVAL_MS;
    for (const [key, expiresAt] of this.recentInbound.entries()) {
      if (expiresAt <= now) this.recentInbound.delete(key);
    }
    if (this.recentInbound.size <= INBOUND_DEDUPE_MAX) return;
    const entries = Array.from(this.recentInbound.entries()).sort((a, b) => a[1] - b[1]);
    const overflow = this.recentInbound.size - INBOUND_DEDUPE_MAX;
    for (let i = 0; i < overflow; i += 1) {
      this.recentInbound.delete(entries[i][0]);
    }
  }

  private seenInbound(key: string): boolean {
    this.cleanupRecentInbound();
    const now = nowMs();
    const existing = this.recentInbound.get(key);
    if (existing && existing > now) return true;
    this.recentInbound.set(key, now + INBOUND_DEDUPE_TTL_MS);
    return false;
  }

  private extractContextInfo(msg: any): any {
    const message = msg?.message || {};
    return (
      message.extendedTextMessage?.contextInfo ||
      message.imageMessage?.contextInfo ||
      message.videoMessage?.contextInfo ||
      message.documentMessage?.contextInfo ||
      message.audioMessage?.contextInfo ||
      message.stickerMessage?.contextInfo ||
      null
    );
  }

  private unwrapNestedMessage(messageRaw: any): any {
    let current = messageRaw;
    for (let i = 0; i < 6; i += 1) {
      if (!current || typeof current !== 'object') return null;
      if (current.ephemeralMessage?.message) {
        current = current.ephemeralMessage.message;
        continue;
      }
      if (current.viewOnceMessage?.message) {
        current = current.viewOnceMessage.message;
        continue;
      }
      if (current.viewOnceMessageV2?.message) {
        current = current.viewOnceMessageV2.message;
        continue;
      }
      if (current.viewOnceMessageV2Extension?.message) {
        current = current.viewOnceMessageV2Extension.message;
        continue;
      }
      if (current.documentWithCaptionMessage?.message) {
        current = current.documentWithCaptionMessage.message;
        continue;
      }
      break;
    }
    return current;
  }

  private extractQuotedMessageText(quotedRaw: any): string | null {
    const message = this.unwrapNestedMessage(quotedRaw);
    if (!message) return null;

    if (typeof message.conversation === 'string' && message.conversation.trim()) {
      return message.conversation.trim();
    }
    if (typeof message.extendedTextMessage?.text === 'string' && message.extendedTextMessage.text.trim()) {
      return message.extendedTextMessage.text.trim();
    }
    if (message.imageMessage) {
      const caption = String(message.imageMessage.caption || '').trim();
      return caption ? `[Image] ${caption}` : '[Image]';
    }
    if (message.videoMessage) {
      const caption = String(message.videoMessage.caption || '').trim();
      return caption ? `[Video] ${caption}` : '[Video]';
    }
    if (message.documentMessage) {
      const caption = String(message.documentMessage.caption || '').trim();
      return caption ? `[Document] ${caption}` : '[Document]';
    }
    if (message.audioMessage) return '[Voice Message]';
    if (message.stickerMessage) return '[Sticker]';
    return null;
  }

  private extractReplyMeta(msg: any): {
    replyToMessageId?: string;
    replyToParticipantJid?: string;
    replyToText?: string;
  } {
    const context = this.extractContextInfo(msg) || {};
    const replyToMessageIdRaw = String(context.stanzaId || '').trim();
    const replyToParticipantJidRaw = normalizeJid(String(context.participant || ''));
    const replyToTextRaw = this.extractQuotedMessageText(context.quotedMessage);

    const replyToMessageId = replyToMessageIdRaw || undefined;
    const replyToParticipantJid = replyToParticipantJidRaw || undefined;
    const replyToText = replyToTextRaw ? limitText(replyToTextRaw, 1_000) : undefined;

    return { replyToMessageId, replyToParticipantJid, replyToText };
  }

  private extractMentionMeta(msg: any, text: string): {
    mentionedJids: string[];
    mentionedBot: boolean;
    replyToBot: boolean;
  } {
    const context = this.extractContextInfo(msg) || {};
    const mentionedRaw = Array.isArray(context.mentionedJid) ? context.mentionedJid : [];
    const mentionedJids = mentionedRaw
      .map((jid: unknown) => normalizeJid(String(jid || '')))
      .filter((jid: string) => jid.length > 0);

    let mentionedBot = false;
    for (const jid of mentionedJids) {
      if (this.selfJids.has(jid)) {
        mentionedBot = true;
        break;
      }
      const token = jidUserToken(jid);
      if (token && this.selfTokens.has(token)) {
        mentionedBot = true;
        break;
      }
    }

    if (!mentionedBot) {
      MENTION_TOKEN_PATTERN.lastIndex = 0;
      for (const match of text.matchAll(MENTION_TOKEN_PATTERN)) {
        const token = match?.[1] || '';
        if (token && this.selfTokens.has(token)) {
          mentionedBot = true;
          break;
        }
      }
    }

    const replyParticipant = normalizeJid(String(context.participant || ''));
    const replyToBot = replyParticipant ? this.selfJids.has(replyParticipant) : false;
    return { mentionedJids, mentionedBot, replyToBot };
  }

  private extractMessageTextAndMedia(msg: any): { text: string | null; media?: InboundMedia } {
    const message = msg?.message;
    if (!message) return { text: null };

    if (typeof message.conversation === 'string' && message.conversation.trim()) {
      return { text: message.conversation.trim() };
    }

    if (typeof message.extendedTextMessage?.text === 'string' && message.extendedTextMessage.text.trim()) {
      return { text: message.extendedTextMessage.text.trim() };
    }

    if (message.imageMessage) {
      const caption = String(message.imageMessage.caption || '').trim();
      return {
        text: caption ? `[Image] ${caption}` : '[Image]',
        media: {
          kind: 'image',
          mimeType: message.imageMessage.mimetype,
        },
      };
    }

    if (message.videoMessage) {
      const caption = String(message.videoMessage.caption || '').trim();
      return {
        text: caption ? `[Video] ${caption}` : '[Video]',
        media: {
          kind: 'video',
          mimeType: message.videoMessage.mimetype,
        },
      };
    }

    if (message.audioMessage) {
      return {
        text: '[Voice Message]',
        media: {
          kind: 'audio',
          mimeType: message.audioMessage.mimetype,
        },
      };
    }

    if (message.documentMessage) {
      const caption = String(message.documentMessage.caption || '').trim();
      return {
        text: caption ? `[Document] ${caption}` : '[Document]',
        media: {
          kind: 'document',
          mimeType: message.documentMessage.mimetype,
          fileName: message.documentMessage.fileName,
        },
      };
    }

    if (message.stickerMessage) {
      return {
        text: '[Sticker]',
        media: {
          kind: 'sticker',
          mimeType: message.stickerMessage.mimetype,
        },
      };
    }

    return { text: null };
  }

  private resolveQr(qr: string): void {
    this.latestQr = qr;
    this.latestQrAt = nowMs();
    for (const waiter of this.qrWaiters) waiter(qr);
    this.qrWaiters.clear();
  }

  private resolveConnected(connected: boolean): void {
    for (const waiter of this.connectWaiters) waiter(connected);
    this.connectWaiters.clear();
  }

  private async getBaileysVersion(): Promise<[number, number, number]> {
    if (!this.baileysVersionCache) {
      const { version } = await fetchLatestBaileysVersion();
      this.baileysVersionCache = version as [number, number, number];
    }
    return this.baileysVersionCache;
  }

  private async connectOnce(): Promise<void> {
    const logger = pino({ level: 'silent' });
    await ensureAuthSecurity(this.options.authDir);

    const { state, saveCreds } = await useMultiFileAuthState(this.options.authDir);
    const version = await this.getBaileysVersion();

    this.updateSelfIds(state);

    this.sock = makeWASocket({
      auth: {
        creds: state.creds,
        keys: makeCacheableSignalKeyStore(state.keys, logger),
      },
      version,
      logger,
      printQRInTerminal: false,
      browser: ['nanobot', 'bridge', VERSION],
      syncFullHistory: false,
      markOnlineOnConnect: true,
    });

    let closedResolve: ((reason: unknown) => void) | null = null;
    const closed = new Promise<unknown>((resolve) => {
      closedResolve = resolve;
    });

    if (this.sock.ws && typeof this.sock.ws.on === 'function') {
      this.sock.ws.on('error', (err: Error) => {
        this.lastError = safeErrorMessage(err);
        this.options.onError(this.lastError);
      });
    }

    this.sock.ev.on('creds.update', async () => {
      await saveCreds();
      await ensureAuthSecurity(this.options.authDir);
    });

    this.sock.ev.on('connection.update', (update: any) => {
      const { connection, lastDisconnect, qr } = update;

      if (qr) {
        qrcode.generate(qr, { small: true });
        this.options.onQR(qr);
        this.resolveQr(qr);
      }

      if (connection === 'open') {
        this.connected = true;
        this.reconnectAttempts = 0;
        this.lastError = undefined;
        // Keep this companion marked online to reduce phone push notifications.
        void this.sock.sendPresenceUpdate('available').catch((err: unknown) => {
          this.lastError = safeErrorMessage(err);
        });
        this.options.onStatus('connected');
        this.resolveConnected(true);
      }

      if (connection === 'close') {
        const statusCode = (lastDisconnect?.error as Boom)?.output?.statusCode;
        this.connected = false;
        this.lastDisconnectStatus = typeof statusCode === 'number' ? statusCode : undefined;
        this.options.onStatus('disconnected', {
          statusCode: this.lastDisconnectStatus,
        });
        this.resolveConnected(false);
        if (closedResolve) {
          closedResolve(lastDisconnect?.error ?? new Error('connection closed'));
          closedResolve = null;
        }
      }
    });

    this.sock.ev.on('messages.upsert', async ({ messages, type }: { messages: any[]; type: string }) => {
      if (type !== 'notify' && type !== 'append') return;
      for (const msg of messages) {
        if (!this.running) return;
        if (msg?.key?.fromMe) continue;

        const remoteJidRaw = String(msg?.key?.remoteJid || '');
        if (!remoteJidRaw || remoteJidRaw === 'status@broadcast') continue;

        const chatJid = normalizeJid(remoteJidRaw);
        if (!chatJid) continue;

        const messageId = String(msg?.key?.id || '').trim();
        if (!messageId) continue;

        const dedupeKey = createHash('sha1').update(`${chatJid}:${messageId}`).digest('hex');
        if (this.seenInbound(dedupeKey)) {
          this.droppedInboundDuplicates += 1;
          continue;
        }

        const isGroup = chatJid.endsWith('@g.us');
        const participantJidRaw =
          msg?.key?.participant ||
          msg?.participant ||
          msg?.message?.extendedTextMessage?.contextInfo?.participant ||
          remoteJidRaw;
        const participantJid = normalizeJid(String(participantJidRaw || ''));
        const senderId = jidUserToken(participantJid || chatJid);

        const extracted = this.extractMessageTextAndMedia(msg);
        if (!extracted.text) continue;

        const mention = this.extractMentionMeta(msg, extracted.text);
        const reply = this.extractReplyMeta(msg);
        const tsRaw = msg?.messageTimestamp;
        const timestamp = typeof tsRaw === 'number' ? tsRaw : Number(tsRaw || 0);

        this.lastMessageAt = nowMs();

        this.options.onMessage({
          messageId,
          chatJid,
          participantJid,
          senderId,
          isGroup,
          text: limitText(extracted.text, 8_000),
          timestamp: Number.isFinite(timestamp) ? timestamp : Math.floor(nowMs() / 1000),
          mentionedJids: mention.mentionedJids,
          mentionedBot: mention.mentionedBot,
          replyToBot: mention.replyToBot,
          replyToMessageId: reply.replyToMessageId,
          replyToParticipantJid: reply.replyToParticipantJid,
          replyToText: reply.replyToText,
          media: extracted.media,
        });
      }
    });

    await closed;
  }

  async sendText(to: string, text: string): Promise<{ to: string }> {
    if (!this.sock || !this.connected) {
      throw new Error('Not connected');
    }
    await this.sock.sendMessage(to, { text: limitText(text, 8_000) });
    return { to };
  }

  async sendMedia(input: SendMediaInput): Promise<{ to: string; mimeType: string; bytes: number }> {
    if (!this.sock || !this.connected) {
      throw new Error('Not connected');
    }

    const media = await loadMediaSource(input);
    const kind = mediaKindFromMime(media.mimeType);
    const caption = input.caption ? limitText(input.caption, 2_000) : undefined;

    if (kind === 'image') {
      await this.sock.sendMessage(input.to, {
        image: media.buffer,
        caption,
        mimetype: media.mimeType,
      });
    } else if (kind === 'video') {
      await this.sock.sendMessage(input.to, {
        video: media.buffer,
        caption,
        mimetype: media.mimeType,
      });
    } else if (kind === 'audio') {
      await this.sock.sendMessage(input.to, {
        audio: media.buffer,
        ptt: true,
        mimetype: media.mimeType,
      });
    } else {
      await this.sock.sendMessage(input.to, {
        document: media.buffer,
        fileName: media.fileName || 'file',
        caption,
        mimetype: media.mimeType,
      });
    }

    return { to: input.to, mimeType: media.mimeType, bytes: media.buffer.length };
  }

  async sendPoll(input: SendPollInput): Promise<{ to: string; options: number }> {
    if (!this.sock || !this.connected) {
      throw new Error('Not connected');
    }

    const options = input.options.map((x) => String(x).trim()).filter((x) => x.length > 0);
    if (options.length < 2) {
      throw new Error('Poll requires at least 2 options');
    }

    await this.sock.sendMessage(input.to, {
      poll: {
        name: limitText(input.question, 512),
        values: options.slice(0, 12),
        selectableCount: Math.max(1, Math.min(12, input.maxSelections ?? 1)),
      },
    });

    return { to: input.to, options: options.length };
  }

  async react(input: ReactInput): Promise<{ chatJid: string; messageId: string }> {
    if (!this.sock || !this.connected) {
      throw new Error('Not connected');
    }

    await this.sock.sendMessage(input.chatJid, {
      react: {
        text: input.emoji,
        key: {
          remoteJid: input.chatJid,
          id: input.messageId,
          fromMe: Boolean(input.fromMe),
          participant: input.participantJid,
        },
      },
    });

    return { chatJid: input.chatJid, messageId: input.messageId };
  }

  async updatePresence(input: PresenceUpdateInput): Promise<{ state: PresenceState; chatJid?: string }> {
    if (!this.sock || !this.connected) {
      throw new Error('Not connected');
    }

    const needsChatJid = input.state === 'composing' || input.state === 'paused' || input.state === 'recording';
    if (needsChatJid && !input.chatJid) {
      throw new Error(`Presence state '${input.state}' requires chatJid`);
    }

    // Some deployments/states can stall presence ACKs; don't block bridge responses indefinitely.
    const sendOp = (async (): Promise<void> => {
      if (input.chatJid) {
        await this.sock.sendPresenceUpdate(input.state, input.chatJid);
      } else {
        await this.sock.sendPresenceUpdate(input.state);
      }
    })();
    void sendOp.catch((err: unknown) => {
      this.lastError = safeErrorMessage(err);
      this.options.onError(`presence_update_failed: ${this.lastError}`);
    });
    await Promise.race([sendOp, sleep(1500)]);

    return { state: input.state, chatJid: input.chatJid };
  }

  async listGroups(ids?: string[]): Promise<Array<{ id: string; subject: string }>> {
    if (!this.sock || !this.connected) {
      throw new Error('Not connected');
    }

    const all = await this.sock.groupFetchAllParticipating();
    const byId = new Map<string, string>();
    for (const [jid, meta] of Object.entries(all || {})) {
      const normalized = normalizeJid(String(jid));
      const subject = String((meta as any)?.subject || '').trim();
      if (normalized && subject) {
        byId.set(normalized, subject);
      }
    }

    const wanted = (ids || []).map((x) => normalizeJid(String(x))).filter((x) => x.length > 0);
    const selected = wanted.length > 0 ? wanted : Array.from(byId.keys());
    return selected
      .map((id) => ({ id, subject: byId.get(id) || '' }))
      .filter((x) => x.subject.length > 0);
  }

  async loginStart(opts: { force?: boolean; timeoutMs?: number } = {}): Promise<{
    started: boolean;
    qr?: string;
    connected: boolean;
    status: string;
  }> {
    if (this.connected && !opts.force) {
      return { started: false, connected: true, status: 'already_connected' };
    }

    if (!this.running) {
      await this.start();
    }

    const maxQrAgeMs = 120_000;
    if (this.latestQr && nowMs() - this.latestQrAt <= maxQrAgeMs) {
      return {
        started: true,
        qr: this.latestQr,
        connected: this.connected,
        status: 'qr_ready',
      };
    }

    const timeoutMs = Math.max(2_000, Math.min(120_000, opts.timeoutMs ?? 30_000));
    const qr = await this.waitForQr(timeoutMs);
    return {
      started: true,
      qr,
      connected: this.connected,
      status: 'qr_ready',
    };
  }

  async loginWait(opts: { timeoutMs?: number } = {}): Promise<{ connected: boolean; status: string }> {
    if (this.connected) {
      return { connected: true, status: 'connected' };
    }

    const timeoutMs = Math.max(2_000, Math.min(300_000, opts.timeoutMs ?? 120_000));
    const connected = await this.waitForConnected(timeoutMs);
    return {
      connected,
      status: connected ? 'connected' : 'timeout',
    };
  }

  private waitForQr(timeoutMs: number): Promise<string> {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.qrWaiters.delete(onQr);
        reject(new Error('Timed out waiting for QR'));
      }, timeoutMs);

      const onQr = (qr: string) => {
        clearTimeout(timer);
        this.qrWaiters.delete(onQr);
        resolve(qr);
      };

      this.qrWaiters.add(onQr);
    });
  }

  private waitForConnected(timeoutMs: number): Promise<boolean> {
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        this.connectWaiters.delete(onConnected);
        resolve(false);
      }, timeoutMs);

      const onConnected = (connected: boolean) => {
        if (!connected) return;
        clearTimeout(timer);
        this.connectWaiters.delete(onConnected);
        resolve(true);
      };

      this.connectWaiters.add(onConnected);
    });
  }

  async logout(): Promise<{ loggedOut: boolean }> {
    if (!this.sock) {
      return { loggedOut: false };
    }
    try {
      await this.sock.logout();
    } catch {
      // Ignore: connection might already be gone.
    }
    try {
      this.sock.end(undefined);
    } catch {
      // Ignore.
    }
    this.connected = false;
    return { loggedOut: true };
  }

  health(): WhatsAppHealth {
    return {
      connected: this.connected,
      running: this.running,
      reconnectAttempts: this.reconnectAttempts,
      lastDisconnectStatus: this.lastDisconnectStatus,
      lastError: this.lastError,
      lastMessageAt: this.lastMessageAt,
      droppedInboundDuplicates: this.droppedInboundDuplicates,
      dedupeCacheSize: this.recentInbound.size,
    };
  }

  async stop(): Promise<void> {
    this.running = false;
    this.resolveConnected(false);

    if (this.sock) {
      try {
        this.sock.end(undefined);
      } catch {
        // Ignore end failures.
      }
      this.sock = null;
    }

    if (this.loopTask) {
      await Promise.race([this.loopTask, sleep(1_000)]);
      this.loopTask = null;
    }
  }
}
