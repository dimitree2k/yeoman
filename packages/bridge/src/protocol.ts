import { createHash } from 'node:crypto';

export const PROTOCOL_VERSION = 6 as const;
export const MAX_BRIDGE_FRAME_BYTES = 262_144 as const;

export const REPLAYABLE_EVENT_TYPES = [
  'message',
  'edit',
  'delete',
  'reaction',
  'receipt',
  'membership_change',
  'membership_snapshot',
  'group_subject',
  'group_description',
] as const;

export const MEDIA_METADATA_FIELDS = [
  'kind',
  'mimeType',
  'fileName',
  'bytes',
  'path',
  'ref',
  'sha256',
  'hash',
] as const;

export const OUTBOUND_MESSAGE_ID_FIELDS = ['providerMessageId', 'clientMessageId'] as const;

const TOKEN_JSON_RE = /("token"\s*:\s*")[^"]*(")/gi;
const TOKEN_ENV_RE = /(BRIDGE_TOKEN=)[^\s]+/gi;

export type BridgeCommandType =
  | 'send_text'
  | 'send_media'
  | 'forward_message'
  | 'send_poll'
  | 'delete_message'
  | 'react'
  | 'presence_update'
  | 'list_groups'
  | 'login_start'
  | 'login_wait'
  | 'logout'
  | 'lookup_message'
  | 'subscribe_events'
  | 'ack_event'
  | 'health';

export type BridgeEventType =
  | 'message'
  | 'edit'
  | 'delete'
  | 'reaction'
  | 'receipt'
  | 'membership_change'
  | 'membership_snapshot'
  | 'group_subject'
  | 'group_description'
  | 'status'
  | 'qr'
  | 'error'
  | 'response';

export interface ProtocolError {
  code:
    | 'ERR_PROTOCOL_VERSION'
    | 'ERR_SCHEMA'
    | 'ERR_AUTH'
    | 'ERR_UNSUPPORTED'
    | 'ERR_PAYLOAD_TOO_LARGE'
    | 'ERR_QUEUE_OVERFLOW'
    | 'ERR_FORWARD_UNAVAILABLE'
    | 'ERR_INTERNAL';
  message: string;
  retryable: boolean;
}

export interface SendTextPayload {
  to: string;
  text: string;
  replyToMessageId?: string;
  mentions?: string[];
  historyMentionsResolved?: boolean;
  clientMessageId?: string;
}

export interface SendMediaPayload {
  to: string;
  mediaUrl?: string;
  mediaBase64?: string;
  mediaPath?: string;
  mimeType?: string;
  fileName?: string;
  caption?: string;
  replyToMessageId?: string;
  mentions?: string[];
  historyMentionsResolved?: boolean;
  clientMessageId?: string;
}

export interface ForwardMessagePayload {
  to: string;
  sourceChatJid: string;
  sourceMessageId: string;
  clientMessageId?: string;
}

export interface SendPollPayload {
  to: string;
  question: string;
  options: string[];
  maxSelections?: number;
  clientMessageId?: string;
}

export interface ReactPayload {
  chatJid: string;
  messageId: string;
  emoji: string;
  participantJid?: string;
  fromMe?: boolean;
  clientMessageId?: string;
}

export interface DeleteMessagePayload {
  chatJid: string;
  messageId: string;
}

export interface PresenceUpdatePayload {
  state: 'available' | 'unavailable' | 'composing' | 'paused' | 'recording';
  chatJid?: string;
}

export interface ListGroupsPayload {
  ids?: string[];
}

export interface LoginStartPayload {
  force?: boolean;
  timeoutMs?: number;
}

export interface LoginWaitPayload {
  timeoutMs?: number;
}

export interface LookupMessagePayload {
  chatJid: string;
  messageId: string;
}

export interface SubscribeEventsPayload {
  [k: string]: never;
}

export interface AckEventPayload {
  eventId: string;
}

/**
 * Answer about one provider message. ``found`` comes from a proven local source only;
 * ``unsupported`` means the bridge has no authority to answer (it is never a claim that
 * the message is absent), and ``absent`` is reserved for a query that really came back
 * negative.
 */
export interface LookupMessageResult {
  status: 'found' | 'absent' | 'unsupported';
  messageId?: string;
}

export interface EmptyPayload {
  [k: string]: never;
}

export interface BridgeCommandEnvelope {
  version: typeof PROTOCOL_VERSION;
  type: BridgeCommandType;
  token: string;
  requestId?: string;
  accountId?: string;
  payload: Record<string, unknown>;
}

export interface BridgeEventEnvelope {
  version: typeof PROTOCOL_VERSION;
  type: BridgeEventType;
  ts: number;
  accountId: string;
  eventId?: string;
  eventKey?: string;
  observedAt?: number;
  requestId?: string;
  payload: Record<string, unknown>;
}

export interface BridgeMediaMetadata {
  kind: 'image' | 'video' | 'audio' | 'document' | 'sticker';
  mimeType?: string;
  fileName?: string;
  bytes?: number;
  path?: string;
  ref?: string;
  sha256?: string;
  hash?: string;
}

export interface BridgeSendResult {
  to: string;
  messageId?: string;
  providerMessageId?: string;
  clientMessageId?: string;
}

/** Optional additions to v5; old results without body fields remain readable. */
export interface BridgePollResult extends BridgeSendResult {
  options: number;
  poll: { name: string; values: string[]; selectableCount: number };
}

export interface BridgeForwardContent {
  text: string | null;
  caption: string | null;
  media: Record<string, unknown> | null;
  forwarded: true;
  sourceChatJid: string;
  sourceMessageId: string;
  provenance: 'sent' | 'source';
}

export interface BridgeForwardResult extends BridgeSendResult {
  content: BridgeForwardContent;
}

export const FORWARD_CONTENT_FIELDS = [
  'text', 'caption', 'media', 'forwarded', 'sourceChatJid', 'sourceMessageId', 'provenance',
] as const;
export const POLL_RESULT_FIELDS = ['name', 'values', 'selectableCount'] as const;

export function validateOutboundResult(type: string, result: Record<string, unknown>): boolean {
  const wrapper = type === 'forward_message' ? 'forwarded' : type === 'delete_message' ? 'deleted' :
    type === 'react' ? 'reacted' : type === 'send_poll' ? 'sent' : undefined;
  if (!wrapper) return true;
  const body = result[wrapper];
  if (!isRecord(body)) return false;
  if (type === 'delete_message') return Boolean(asString(body.chatJid) && asString(body.messageId));
  if (type === 'react') return Boolean(asString(body.chatJid) && asString(body.messageId));
  if (!asString(body.to)) return false;
  for (const field of ['messageId', 'providerMessageId', 'clientMessageId']) {
    if (body[field] !== undefined && !asString(body[field])) return false;
  }
  if (type === 'send_poll') {
    if (!Number.isSafeInteger(body.options) || (body.options as number) < 2) return false;
    if (body.poll === undefined) return true; // legacy v5
    const poll = body.poll;
    return isRecord(poll) && Object.keys(poll).length === POLL_RESULT_FIELDS.length &&
      Object.keys(poll).every(k => (POLL_RESULT_FIELDS as readonly string[]).includes(k)) &&
      typeof poll.name === 'string' && asString(poll.name) !== null && poll.name.length <= 512 && Array.isArray(poll.values) &&
      poll.values.length >= 2 && poll.values.length <= 12 && poll.values.every(x => asString(x) !== null) &&
      Number.isSafeInteger(poll.selectableCount) && (poll.selectableCount as number) >= 1 &&
      (poll.selectableCount as number) <= 12;
  }
  if (body.content === undefined) return true; // legacy v5
  const content = body.content;
  if (!isRecord(content) || Object.keys(content).length !== FORWARD_CONTENT_FIELDS.length ||
      !Object.keys(content).every(k => (FORWARD_CONTENT_FIELDS as readonly string[]).includes(k)) ||
      ![content.text, content.caption].every(x => x === null || typeof x === 'string') ||
      content.forwarded !== true || !asString(content.sourceChatJid) || !asString(content.sourceMessageId) ||
      typeof content.provenance !== 'string' || !['sent', 'source'].includes(content.provenance)) return false;
  const media = content.media;
  return media === null || (isRecord(media) && ['image', 'video', 'audio', 'document', 'sticker'].includes(String(media.kind)) &&
    Object.entries(media).every(([key, value]) => (MEDIA_METADATA_FIELDS as readonly string[]).includes(key) &&
      (key === 'bytes' ? Number.isSafeInteger(value) && (value as number) >= 0 : typeof value === 'string')));
}

export function validGroupMetadata(value: unknown): value is Record<string, unknown> {
  if (!isRecord(value) || !Object.keys(value).every(k =>
      ['chatJid', 'value', 'actorJid', 'occurredMs', 'observedAtMs', 'snapshot'].includes(k))) return false;
  const chat = asString(value.chatJid);
  return Boolean(chat && chat.endsWith('@g.us') && chat.length <= 128) &&
    typeof value.value === 'string' && value.value.length <= MAX_BRIDGE_FRAME_BYTES &&
    typeof value.snapshot === 'boolean' && Number.isSafeInteger(value.observedAtMs) &&
    (value.observedAtMs as number) >= 0 &&
    (!('actorJid' in value) || asString(value.actorJid) !== null) &&
    (!('occurredMs' in value) || Number.isSafeInteger(value.occurredMs) && (value.occurredMs as number) >= 0) &&
    (!value.snapshot || !('actorJid' in value) && !('occurredMs' in value));
}

export interface ProviderEventIdentity {
  eventId: string;
  eventKey: string;
}

function identityPart(value: unknown): string {
  return typeof value === 'string' ? value.trim() : String(value ?? '').trim();
}

/** Stable identity for one edit revision, using the replacement snapshot when no revision exists. */
function deriveEditRevisionIdentity(payload: Record<string, unknown>): string {
  const revision = identityPart(payload.revision ?? payload.editRevision ?? '');
  if (revision) return revision;

  const timestamp = identityPart(payload.timestamp ?? payload.editTimestamp ?? '');
  const text = typeof payload.text === 'string' ? payload.text : '';
  const mediaMetadata: Array<[string, string | number]> = [];
  if (payload.media && typeof payload.media === 'object' && !Array.isArray(payload.media)) {
    const media = payload.media as Record<string, unknown>;
    for (const field of MEDIA_METADATA_FIELDS) {
      const value = media[field];
      if (typeof value === 'string' || typeof value === 'number') {
        mediaMetadata.push([field, value]);
      }
    }
  }
  const snapshot = JSON.stringify([timestamp, text, mediaMetadata]);
  const digest = createHash('sha256').update(snapshot ?? 'null', 'utf8').digest('hex').slice(0, 32);
  return `legacy:${digest}`;
}

/** Local dedupe identity for edits, scoped to the chat and target message. */
export function deriveEditSignalIdentity(payload: Record<string, unknown>): string | undefined {
  const chat = identityPart(payload.chatJid ?? payload.chat_jid ?? payload.chat);
  const messageId = identityPart(payload.messageId ?? payload.message_id ?? payload.id);
  if (!chat || !messageId) return undefined;
  return [chat, messageId, deriveEditRevisionIdentity(payload)]
    .map((value) => encodeURIComponent(value))
    .join(':');
}

/** Derive replay identity from provider ids/revisions, with a stable edit snapshot fallback. */
export function deriveProviderEventIdentity(
  type: (typeof REPLAYABLE_EVENT_TYPES)[number],
  accountId: string,
  payload: Record<string, unknown>,
): ProviderEventIdentity | undefined {
  const account = identityPart(accountId);
  const chat = identityPart(payload.chatJid ?? payload.chat_jid ?? payload.chat);
  if (!account || !chat) return undefined;

  if (type === 'membership_change') {
    const changeId = identityPart(payload.changeId);
    const copyId = identityPart(payload.sourceCopyId);
    if (!changeId || !copyId) return undefined;
    const eventKey = `whatsapp:${[account, chat, type, changeId].map(encodeURIComponent).join(':')}`;
    const copyKey = `${eventKey}:${encodeURIComponent(copyId)}`;
    return {
      eventKey,
      eventId: `wa_${createHash('sha256').update(copyKey, 'utf8').digest('hex').slice(0, 32)}`,
    };
  }

  let providerIdentity: string[];
  if (type === 'group_subject' || type === 'group_description') {
    if (!validGroupMetadata(payload)) return undefined;
    // Bound the key because outbox staging includes its encoded form in a filename.
    const digest = createHash('sha256').update(JSON.stringify([account, chat, type, payload.snapshot,
      payload.occurredMs ?? payload.observedAtMs, identityPart(payload.actorJid), payload.value]), 'utf8').digest('hex');
    const eventKey = `whatsapp:${type}:${digest}`;
    return { eventKey, eventId: `wa_${createHash('sha256').update(eventKey, 'utf8').digest('hex').slice(0, 32)}` };
  } else if (type === 'membership_snapshot') {
    const timestamp = payload.snapshotAtMs;
    if (typeof timestamp !== 'number' || !Number.isSafeInteger(timestamp) || timestamp < 0) return undefined;
    providerIdentity = [String(timestamp)];
  } else if (type === 'message') {
    const messageId = identityPart(payload.messageId ?? payload.message_id ?? payload.id);
    if (!messageId) return undefined;
    providerIdentity = [messageId];
  } else if (type === 'edit') {
    const messageId = identityPart(payload.messageId ?? payload.message_id ?? payload.id);
    if (!messageId) return undefined;
    providerIdentity = [messageId, deriveEditRevisionIdentity(payload)];
  } else if (type === 'delete') {
    const messageId = identityPart(
      payload.messageId ?? payload.message_id ?? payload.targetMessageId ?? payload.id,
    );
    if (!messageId) return undefined;
    providerIdentity = [messageId];
  } else if (type === 'reaction') {
    const target = identityPart(payload.targetMessageId ?? payload.target_message_id ?? payload.messageId);
    const sender = identityPart(payload.senderId ?? payload.sender ?? payload.participantJid);
    if (!target) return undefined;
    providerIdentity = [target, sender];
  } else {
    const messageId = identityPart(payload.messageId ?? payload.message_id ?? payload.id);
    if (!messageId) return undefined;
    const recipient = identityPart(
      payload.recipientJid ?? payload.recipient ?? payload.participantJid ?? payload.to,
    );
    const status = identityPart(payload.status ?? payload.receiptType ?? 'delivered').toLowerCase();
    providerIdentity = [messageId, recipient, status];
  }

  const encoded = [account, chat, type, ...providerIdentity].map((value) => encodeURIComponent(value));
  const eventKey = `whatsapp:${encoded.join(':')}`;
  const eventId = `wa_${createHash('sha256').update(eventKey, 'utf8').digest('hex').slice(0, 32)}`;
  return { eventId, eventKey };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function err(
  code: ProtocolError['code'],
  message: string,
  retryable = false,
): { ok: false; error: ProtocolError } {
  return { ok: false, error: { code, message, retryable } };
}

function asString(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : null;
}

function asOptionalString(value: unknown): string | undefined {
  if (value === undefined || value === null) return undefined;
  return asString(value) ?? undefined;
}

function asOptionalBool(value: unknown): boolean | undefined {
  if (value === undefined || value === null) return undefined;
  return typeof value === 'boolean' ? value : undefined;
}

function asOptionalNumber(value: unknown): number | undefined {
  if (value === undefined || value === null) return undefined;
  if (typeof value !== 'number' || !Number.isFinite(value)) return undefined;
  return value;
}

function asOptionalStringArray(value: unknown): string[] | undefined {
  if (value === undefined || value === null) return undefined;
  if (!Array.isArray(value)) return undefined;
  const out: string[] = [];
  for (const item of value) {
    const s = asString(item);
    if (!s) return undefined;
    out.push(s);
  }
  return out;
}

const CLIENT_MESSAGE_ID_RE = /^[A-Za-z0-9_-]{8,128}$/;

function asOptionalClientMessageId(value: unknown): string | undefined | null {
  if (value === undefined || value === null) return undefined;
  const parsed = asString(value);
  if (!parsed || !CLIENT_MESSAGE_ID_RE.test(parsed)) return null;
  return parsed;
}

export function validHistoryMentions(payload: Record<string, unknown>): boolean {
  if (payload.historyMentionsResolved !== undefined && typeof payload.historyMentionsResolved !== 'boolean') return false;
  if (payload.historyMentionsResolved !== true) return true;
  const mentions = payload.mentions === undefined ? [] : payload.mentions;
  return Array.isArray(mentions) && mentions.every(jid => typeof jid === 'string' && /^[0-9]+@s\.whatsapp\.net$/.test(jid));
}

function parseSendText(payload: Record<string, unknown>): SendTextPayload | null {
  const to = asString(payload.to);
  const text = asString(payload.text);
  const replyToMessageId = asOptionalString(payload.replyToMessageId);
  const mentions = asOptionalStringArray(payload.mentions);
  const clientMessageId = asOptionalClientMessageId(payload.clientMessageId);
  if (!to || !text) return null;
  if (payload.mentions !== undefined && !mentions) return null;
  if (!validHistoryMentions(payload)) return null;
  if (clientMessageId === null) return null;
  return { to, text, replyToMessageId, mentions, clientMessageId, historyMentionsResolved: payload.historyMentionsResolved as boolean | undefined };
}

function parseSendMedia(payload: Record<string, unknown>): SendMediaPayload | null {
  const to = asString(payload.to);
  if (!to) return null;
  const mediaUrl = asOptionalString(payload.mediaUrl);
  const mediaBase64 = asOptionalString(payload.mediaBase64);
  const mediaPath = asOptionalString(payload.mediaPath);
  const mimeType = asOptionalString(payload.mimeType);
  const fileName = asOptionalString(payload.fileName);
  const caption = asOptionalString(payload.caption);
  const replyToMessageId = asOptionalString(payload.replyToMessageId);
  const mentions = asOptionalStringArray(payload.mentions);
  const clientMessageId = asOptionalClientMessageId(payload.clientMessageId);
  if (!mediaUrl && !mediaBase64 && !mediaPath) return null;
  if (payload.mentions !== undefined && !mentions) return null;
  if (!validHistoryMentions(payload)) return null;
  if (clientMessageId === null) return null;
  return {
    to,
    mediaUrl,
    mediaBase64,
    mediaPath,
    mimeType,
    fileName,
    caption,
    replyToMessageId,
    mentions,
    historyMentionsResolved: payload.historyMentionsResolved as boolean | undefined,
    clientMessageId,
  };
}

function parseForwardMessage(payload: Record<string, unknown>): ForwardMessagePayload | null {
  const to = asString(payload.to);
  const sourceChatJid = asString(payload.sourceChatJid);
  const sourceMessageId = asString(payload.sourceMessageId);
  const clientMessageId = asOptionalClientMessageId(payload.clientMessageId);
  if (!to || !sourceChatJid || !sourceMessageId || clientMessageId === null) return null;
  return { to, sourceChatJid, sourceMessageId, clientMessageId };
}

function parseSendPoll(payload: Record<string, unknown>): SendPollPayload | null {
  const to = asString(payload.to);
  const question = asString(payload.question);
  const options = asOptionalStringArray(payload.options);
  const maxSelections = asOptionalNumber(payload.maxSelections);
  const clientMessageId = asOptionalClientMessageId(payload.clientMessageId);
  if (!to || !question || !options || options.length < 2) return null;
  if (maxSelections !== undefined && (!Number.isInteger(maxSelections) || maxSelections < 1)) {
    return null;
  }
  if (clientMessageId === null) return null;
  return { to, question, options, maxSelections, clientMessageId };
}

function parseReact(payload: Record<string, unknown>): ReactPayload | null {
  const chatJid = asString(payload.chatJid);
  const messageId = asString(payload.messageId);
  if (!chatJid || !messageId) return null;
  const emoji = typeof payload.emoji === 'string' ? payload.emoji : '';
  const participantJid = asOptionalString(payload.participantJid);
  const fromMe = asOptionalBool(payload.fromMe);
  const clientMessageId = asOptionalClientMessageId(payload.clientMessageId);
  if (clientMessageId === null) return null;
  return { chatJid, messageId, emoji, participantJid, fromMe, clientMessageId };
}

function parseDeleteMessage(payload: Record<string, unknown>): DeleteMessagePayload | null {
  const chatJid = asString(payload.chatJid);
  const messageId = asString(payload.messageId);
  if (!chatJid || !messageId) return null;
  return { chatJid, messageId };
}

function parsePresenceUpdate(payload: Record<string, unknown>): PresenceUpdatePayload | null {
  const stateRaw = asString(payload.state);
  const chatJid = asOptionalString(payload.chatJid);
  if (!stateRaw) return null;

  const state = stateRaw as PresenceUpdatePayload['state'];
  const validStates = new Set<PresenceUpdatePayload['state']>([
    'available',
    'unavailable',
    'composing',
    'paused',
    'recording',
  ]);
  if (!validStates.has(state)) return null;

  if ((state === 'composing' || state === 'paused' || state === 'recording') && !chatJid) {
    return null;
  }

  return { state, chatJid };
}

function parseListGroups(payload: Record<string, unknown>): ListGroupsPayload | null {
  const ids = asOptionalStringArray(payload.ids);
  if (payload.ids !== undefined && !ids) return null;
  return { ids };
}

function parseLoginStart(payload: Record<string, unknown>): LoginStartPayload | null {
  const force = asOptionalBool(payload.force);
  const timeoutMs = asOptionalNumber(payload.timeoutMs);
  if (payload.force !== undefined && force === undefined) return null;
  if (payload.timeoutMs !== undefined) {
    if (timeoutMs === undefined || !Number.isInteger(timeoutMs) || timeoutMs < 1000) return null;
  }
  return { force, timeoutMs };
}

function parseLoginWait(payload: Record<string, unknown>): LoginWaitPayload | null {
  const timeoutMs = asOptionalNumber(payload.timeoutMs);
  if (payload.timeoutMs !== undefined) {
    if (timeoutMs === undefined || !Number.isInteger(timeoutMs) || timeoutMs < 1000) return null;
  }
  return { timeoutMs };
}

function parseSubscribeEvents(payload: Record<string, unknown>): SubscribeEventsPayload | null {
  return Object.keys(payload).length === 0 ? {} : null;
}

function parseAckEvent(payload: Record<string, unknown>): AckEventPayload | null {
  if (Object.keys(payload).length !== 1) return null;
  const eventId = asString(payload.eventId);
  return eventId ? { eventId } : null;
}

export function parseBridgeCommand(
  value: unknown,
): { ok: true; command: BridgeCommandEnvelope } | { ok: false; error: ProtocolError } {
  if (!isRecord(value)) {
    return err('ERR_SCHEMA', 'Command envelope must be an object');
  }

  if (value.version !== PROTOCOL_VERSION) {
    return err('ERR_PROTOCOL_VERSION', `Expected version ${PROTOCOL_VERSION}`);
  }

  const type = asString(value.type);
  if (!type) {
    return err('ERR_SCHEMA', 'Missing command type');
  }

  const token = asString(value.token);
  if (!token) {
    return err('ERR_AUTH', 'Missing bridge token');
  }

  const requestId = asOptionalString(value.requestId);
  const accountId = asOptionalString(value.accountId);
  if (!isRecord(value.payload)) {
    return err('ERR_SCHEMA', 'Payload must be an object');
  }
  const payload = value.payload;

  const typed = type as BridgeCommandType;
  let validPayload = false;

  if (typed === 'send_text') validPayload = Boolean(parseSendText(payload));
  else if (typed === 'send_media') validPayload = Boolean(parseSendMedia(payload));
  else if (typed === 'forward_message') validPayload = Boolean(parseForwardMessage(payload));
  else if (typed === 'send_poll') validPayload = Boolean(parseSendPoll(payload));
  else if (typed === 'delete_message') validPayload = Boolean(parseDeleteMessage(payload));
  else if (typed === 'react') validPayload = Boolean(parseReact(payload));
  else if (typed === 'presence_update') validPayload = Boolean(parsePresenceUpdate(payload));
  else if (typed === 'lookup_message')
    validPayload =
      typeof (payload as Record<string, unknown>).chatJid === 'string' &&
      typeof (payload as Record<string, unknown>).messageId === 'string';
  else if (typed === 'list_groups') validPayload = Boolean(parseListGroups(payload));
  else if (typed === 'login_start') validPayload = Boolean(parseLoginStart(payload));
  else if (typed === 'login_wait') validPayload = Boolean(parseLoginWait(payload));
  else if (typed === 'subscribe_events') validPayload = Boolean(parseSubscribeEvents(payload));
  else if (typed === 'ack_event') validPayload = Boolean(parseAckEvent(payload));
  else if (typed === 'logout' || typed === 'health') validPayload = true;
  else return err('ERR_UNSUPPORTED', `Unsupported command: ${type}`);

  if (!validPayload) {
    return err('ERR_SCHEMA', `Invalid payload for command: ${typed}`);
  }

  return {
    ok: true,
    command: {
      version: PROTOCOL_VERSION,
      type: typed,
      token,
      requestId,
      accountId,
      payload,
    },
  };
}

export function parseSendTextPayload(payload: Record<string, unknown>): SendTextPayload {
  const parsed = parseSendText(payload);
  if (!parsed) throw new Error('Invalid send_text payload');
  return parsed;
}

export function parseSendMediaPayload(payload: Record<string, unknown>): SendMediaPayload {
  const parsed = parseSendMedia(payload);
  if (!parsed) throw new Error('Invalid send_media payload');
  return parsed;
}

export function parseForwardMessagePayload(payload: Record<string, unknown>): ForwardMessagePayload {
  const parsed = parseForwardMessage(payload);
  if (!parsed) throw new Error('Invalid forward_message payload');
  return parsed;
}

export function parseSendPollPayload(payload: Record<string, unknown>): SendPollPayload {
  const parsed = parseSendPoll(payload);
  if (!parsed) throw new Error('Invalid send_poll payload');
  return parsed;
}

export function parseReactPayload(payload: Record<string, unknown>): ReactPayload {
  const parsed = parseReact(payload);
  if (!parsed) throw new Error('Invalid react payload');
  return parsed;
}

export function parseDeleteMessagePayload(payload: Record<string, unknown>): DeleteMessagePayload {
  const parsed = parseDeleteMessage(payload);
  if (!parsed) throw new Error('Invalid delete_message payload');
  return parsed;
}

export function parsePresenceUpdatePayload(payload: Record<string, unknown>): PresenceUpdatePayload {
  const parsed = parsePresenceUpdate(payload);
  if (!parsed) throw new Error('Invalid presence_update payload');
  return parsed;
}

export function parseListGroupsPayload(payload: Record<string, unknown>): ListGroupsPayload {
  const parsed = parseListGroups(payload);
  if (!parsed) throw new Error('Invalid list_groups payload');
  return parsed;
}

export function parseLoginStartPayload(payload: Record<string, unknown>): LoginStartPayload {
  const parsed = parseLoginStart(payload);
  if (!parsed) throw new Error('Invalid login_start payload');
  return parsed;
}

export function parseLoginWaitPayload(payload: Record<string, unknown>): LoginWaitPayload {
  const parsed = parseLoginWait(payload);
  if (!parsed) throw new Error('Invalid login_wait payload');
  return parsed;
}

export function parseSubscribeEventsPayload(payload: Record<string, unknown>): SubscribeEventsPayload {
  const parsed = parseSubscribeEvents(payload);
  if (!parsed) throw new Error('Invalid subscribe_events payload');
  return parsed;
}

export function parseAckEventPayload(payload: Record<string, unknown>): AckEventPayload {
  const parsed = parseAckEvent(payload);
  if (!parsed) throw new Error('Invalid ack_event payload');
  return parsed;
}

export function createEventEnvelope(params: {
  type: BridgeEventType;
  accountId?: string;
  eventId?: string;
  eventKey?: string;
  observedAt?: number;
  requestId?: string;
  payload?: Record<string, unknown>;
}): BridgeEventEnvelope {
  return {
    version: PROTOCOL_VERSION,
    type: params.type,
    ts: Date.now(),
    accountId: params.accountId ?? 'default',
    eventId: params.eventId,
    eventKey: params.eventKey,
    observedAt: params.observedAt,
    requestId: params.requestId,
    payload: params.payload ?? {},
  };
}

export function createOkResponse(params: {
  requestId?: string;
  accountId?: string;
  result?: Record<string, unknown>;
}): BridgeEventEnvelope {
  return createEventEnvelope({
    type: 'response',
    requestId: params.requestId,
    accountId: params.accountId,
    payload: {
      ok: true,
      result: params.result ?? {},
    },
  });
}

export function createErrorResponse(params: {
  requestId?: string;
  accountId?: string;
  error: ProtocolError;
}): BridgeEventEnvelope {
  return createEventEnvelope({
    type: 'response',
    requestId: params.requestId,
    accountId: params.accountId,
    payload: {
      ok: false,
      error: params.error,
    },
  });
}

export function asProtocolError(errUnknown: unknown): ProtocolError {
  const sanitize = (message: string): string =>
    message.replace(TOKEN_JSON_RE, '$1***$2').replace(TOKEN_ENV_RE, '$1***');

  if (isRecord(errUnknown)) {
    const code = asString(errUnknown.code);
    const message = asString(errUnknown.message);
    const retryable = Boolean(errUnknown.retryable);
    if (code && message) {
      return {
        code: (code as ProtocolError['code']) ?? 'ERR_INTERNAL',
        message: sanitize(message),
        retryable,
      };
    }
  }
  if (errUnknown instanceof Error) {
    return { code: 'ERR_INTERNAL', message: sanitize(errUnknown.message), retryable: false };
  }
  return { code: 'ERR_INTERNAL', message: sanitize(String(errUnknown)), retryable: false };
}

export function isLoopbackAddress(addr: string | undefined): boolean {
  if (!addr) return false;
  return (
    addr === '127.0.0.1' ||
    addr === '::1' ||
    addr === '::ffff:127.0.0.1' ||
    addr.startsWith('::ffff:127.')
  );
}
