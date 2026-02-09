export const PROTOCOL_VERSION = 2 as const;

const TOKEN_JSON_RE = /("token"\s*:\s*")[^"]*(")/gi;
const TOKEN_ENV_RE = /(BRIDGE_TOKEN=)[^\s]+/gi;

export type BridgeCommandType =
  | 'send_text'
  | 'send_media'
  | 'send_poll'
  | 'react'
  | 'presence_update'
  | 'list_groups'
  | 'login_start'
  | 'login_wait'
  | 'logout'
  | 'health';

export type BridgeEventType = 'message' | 'status' | 'qr' | 'error' | 'response';

export interface ProtocolError {
  code:
    | 'ERR_PROTOCOL_VERSION'
    | 'ERR_SCHEMA'
    | 'ERR_AUTH'
    | 'ERR_UNSUPPORTED'
    | 'ERR_PAYLOAD_TOO_LARGE'
    | 'ERR_QUEUE_OVERFLOW'
    | 'ERR_INTERNAL';
  message: string;
  retryable: boolean;
}

export interface SendTextPayload {
  to: string;
  text: string;
}

export interface SendMediaPayload {
  to: string;
  mediaUrl?: string;
  mediaBase64?: string;
  mimeType?: string;
  fileName?: string;
  caption?: string;
}

export interface SendPollPayload {
  to: string;
  question: string;
  options: string[];
  maxSelections?: number;
}

export interface ReactPayload {
  chatJid: string;
  messageId: string;
  emoji: string;
  participantJid?: string;
  fromMe?: boolean;
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
  requestId?: string;
  payload: Record<string, unknown>;
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

function parseSendText(payload: Record<string, unknown>): SendTextPayload | null {
  const to = asString(payload.to);
  const text = asString(payload.text);
  if (!to || !text) return null;
  return { to, text };
}

function parseSendMedia(payload: Record<string, unknown>): SendMediaPayload | null {
  const to = asString(payload.to);
  if (!to) return null;
  const mediaUrl = asOptionalString(payload.mediaUrl);
  const mediaBase64 = asOptionalString(payload.mediaBase64);
  const mimeType = asOptionalString(payload.mimeType);
  const fileName = asOptionalString(payload.fileName);
  const caption = asOptionalString(payload.caption);
  if (!mediaUrl && !mediaBase64) return null;
  return { to, mediaUrl, mediaBase64, mimeType, fileName, caption };
}

function parseSendPoll(payload: Record<string, unknown>): SendPollPayload | null {
  const to = asString(payload.to);
  const question = asString(payload.question);
  const options = asOptionalStringArray(payload.options);
  const maxSelections = asOptionalNumber(payload.maxSelections);
  if (!to || !question || !options || options.length < 2) return null;
  if (maxSelections !== undefined && (!Number.isInteger(maxSelections) || maxSelections < 1)) {
    return null;
  }
  return { to, question, options, maxSelections };
}

function parseReact(payload: Record<string, unknown>): ReactPayload | null {
  const chatJid = asString(payload.chatJid);
  const messageId = asString(payload.messageId);
  if (!chatJid || !messageId) return null;
  const emoji = typeof payload.emoji === 'string' ? payload.emoji : '';
  const participantJid = asOptionalString(payload.participantJid);
  const fromMe = asOptionalBool(payload.fromMe);
  return { chatJid, messageId, emoji, participantJid, fromMe };
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
  else if (typed === 'send_poll') validPayload = Boolean(parseSendPoll(payload));
  else if (typed === 'react') validPayload = Boolean(parseReact(payload));
  else if (typed === 'presence_update') validPayload = Boolean(parsePresenceUpdate(payload));
  else if (typed === 'list_groups') validPayload = Boolean(parseListGroups(payload));
  else if (typed === 'login_start') validPayload = Boolean(parseLoginStart(payload));
  else if (typed === 'login_wait') validPayload = Boolean(parseLoginWait(payload));
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

export function createEventEnvelope(params: {
  type: BridgeEventType;
  accountId?: string;
  requestId?: string;
  payload?: Record<string, unknown>;
}): BridgeEventEnvelope {
  return {
    version: PROTOCOL_VERSION,
    type: params.type,
    ts: Date.now(),
    accountId: params.accountId ?? 'default',
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
