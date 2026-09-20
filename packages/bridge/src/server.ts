import { timingSafeEqual } from 'crypto';

import { WebSocketServer, WebSocket, type RawData } from 'ws';

import {
  asProtocolError,
  createErrorResponse,
  createEventEnvelope,
  createOkResponse,
  isLoopbackAddress,
  parseBridgeCommand,
  parseDeleteMessagePayload,
  parseAckEventPayload,
  parseListGroupsPayload,
  parseLoginStartPayload,
  parseLoginWaitPayload,
  parsePresenceUpdatePayload,
  parseReactPayload,
  parseSendMediaPayload,
  parseSendPollPayload,
  parseSendTextPayload,
  PROTOCOL_VERSION,
  type BridgeEventEnvelope,
  type ProtocolError,
} from './protocol.js';
import {
  BridgeOutbox,
  defaultBridgeOutboxDir,
  type ReplayableBridgeEvent,
} from './outbox.js';
import { WhatsAppClient, type InboundMedia, type InboundMessageV2 } from './whatsapp.js';

type ClientMeta = {
  ws: WebSocket;
  inflight: number;
  droppedEvents: number;
  subscribed: boolean;
  replaying: boolean;
  replayQueue: ReplayableBridgeEvent[];
  deliveredEventIds: Set<string>;
  acknowledgedEventIds: Set<string>;
};

const MAX_COMMAND_BYTES = 256 * 1024;
const MAX_INFLIGHT_PER_CLIENT = 20;
const MAX_BUFFERED_BYTES = 2 * 1024 * 1024;

function protocolError(
  code: ProtocolError['code'],
  message: string,
  retryable = false,
): ProtocolError {
  return { code, message, retryable };
}

function constantTimeEqual(a: string, b: string): boolean {
  const aBuf = Buffer.from(a);
  const bBuf = Buffer.from(b);
  if (aBuf.length !== bBuf.length) return false;
  return timingSafeEqual(aBuf, bBuf);
}

function rawDataByteLength(data: RawData): number {
  if (typeof data === 'string') return Buffer.byteLength(data);
  if (data instanceof ArrayBuffer) return data.byteLength;
  if (Array.isArray(data)) return data.reduce((total, chunk) => total + chunk.byteLength, 0);
  return data.byteLength;
}

function rawDataToString(data: RawData): string {
  if (typeof data === 'string') return data;
  if (data instanceof ArrayBuffer) return Buffer.from(data).toString('utf8');
  if (Array.isArray(data)) return Buffer.concat(data).toString('utf8');
  return data.toString('utf8');
}

function mediaMetadata(media: InboundMedia | undefined): Record<string, unknown> | undefined {
  if (!media) return undefined;
  const result: Record<string, unknown> = { kind: media.kind };
  for (const key of ['mimeType', 'fileName', 'path', 'ref', 'sha256', 'hash'] as const) {
    const value = media[key];
    if (typeof value === 'string' && value.trim()) result[key] = value.trim();
  }
  if (typeof media.bytes === 'number' && Number.isSafeInteger(media.bytes) && media.bytes >= 0) {
    result.bytes = media.bytes;
  }
  return result;
}

export class BridgeServer {
  private wss: WebSocketServer | null = null;
  private wa: WhatsAppClient | null = null;
  private readonly clients = new Set<ClientMeta>();
  private readonly outbox: BridgeOutbox;
  private canonicalSubscriber: ClientMeta | null = null;
  private readonly inFlight = new Set<Promise<void>>();
  private intakeStopped = false;
  private persistenceFailure = false;
  private stopping = false;

  constructor(
    private readonly host: string,
    private readonly port: number,
    private readonly authDir: string,
    private readonly mediaIncomingDir: string,
    private readonly mediaOutgoingDir: string,
    private readonly persistInboundAudio: boolean,
    private readonly persistInboundDocuments: boolean,
    private readonly acceptFromMe: boolean,
    private readonly token: string,
    private readonly bridgeVersion: string,
    private readonly buildId: string,
    private readonly readReceipts: boolean,
    private readonly accountId = 'default',
    outboxDir = process.env.BRIDGE_OUTBOX_DIR || defaultBridgeOutboxDir(),
  ) {
    this.outbox = new BridgeOutbox(outboxDir);
  }

  async start(): Promise<void> {
    this.stopping = false;
    this.intakeStopped = false;
    this.persistenceFailure = false;
    await this.outbox.open();
    this.wss = new WebSocketServer({
      host: this.host,
      port: this.port,
      maxPayload: MAX_COMMAND_BYTES,
    });

    console.log(`Bridge server listening on ws://${this.host}:${this.port} (protocol v${PROTOCOL_VERSION})`);

    this.wa = new WhatsAppClient({
      authDir: this.authDir,
      mediaIncomingDir: this.mediaIncomingDir,
      mediaOutgoingDir: this.mediaOutgoingDir,
      persistInboundAudio: this.persistInboundAudio,
      persistInboundDocument: this.persistInboundDocuments,
      acceptFromMe: this.acceptFromMe,
      readReceipts: this.readReceipts,
      accountId: this.accountId,
      onMessage: (msg) => {
        return this.trackProviderEvent(this.broadcastMessage(msg));
      },
      onSignal: (kind, payload) => {
        return this.trackProviderEvent(
          this.broadcastReplayable(
            createEventEnvelope({ type: kind, accountId: this.accountId, payload }),
          ),
        );
      },
      onQR: (qr) =>
        this.broadcastEvent(
          createEventEnvelope({
            type: 'qr',
            accountId: this.accountId,
            payload: { qr },
          }),
        ),
      onStatus: (status, detail) =>
        this.broadcastEvent(
          createEventEnvelope({
            type: 'status',
            accountId: this.accountId,
            payload: { status, ...(detail || {}) },
          }),
        ),
      onError: (error) =>
        this.broadcastEvent(
          createEventEnvelope({
            type: 'error',
            accountId: this.accountId,
            payload: {
              error: {
                code: 'ERR_INTERNAL',
                message: error,
                retryable: true,
              },
            },
          }),
        ),
    });

    await this.wa.start();

    this.wss.on('connection', (ws, req) => {
      const remote = req.socket.remoteAddress;
      if (!isLoopbackAddress(remote)) {
        const event = createErrorResponse({
          error: protocolError('ERR_AUTH', 'Bridge accepts loopback clients only', false),
          accountId: this.accountId,
        });
        ws.send(JSON.stringify(event));
        ws.close(1008, 'loopback only');
        return;
      }

      const meta: ClientMeta = {
        ws,
        inflight: 0,
        droppedEvents: 0,
        subscribed: false,
        replaying: false,
        replayQueue: [],
        deliveredEventIds: new Set(),
        acknowledgedEventIds: new Set(),
      };
      this.clients.add(meta);

      ws.on('message', async (data) => {
        if (meta.inflight >= MAX_INFLIGHT_PER_CLIENT) {
          const event = createErrorResponse({
            error: protocolError('ERR_QUEUE_OVERFLOW', 'Command queue overflow', true),
            accountId: this.accountId,
          });
          ws.send(JSON.stringify(event));
          return;
        }

        const dataBytes = rawDataByteLength(data);
        if (dataBytes > MAX_COMMAND_BYTES) {
          const event = createErrorResponse({
            error: protocolError('ERR_PAYLOAD_TOO_LARGE', 'Payload too large', false),
            accountId: this.accountId,
          });
          ws.send(JSON.stringify(event));
          return;
        }

        meta.inflight += 1;
        try {
          await this.handleClientMessage(meta, rawDataToString(data));
        } finally {
          meta.inflight = Math.max(0, meta.inflight - 1);
        }
      });

      ws.on('close', () => {
        this.handleClientClose(meta);
      });

      ws.on('error', () => {
        this.handleClientClose(meta);
      });
    });
  }

  private async handleClientMessage(meta: ClientMeta, raw: string): Promise<void> {
    let parsedJson: unknown;
    try {
      parsedJson = JSON.parse(raw);
    } catch {
      const event = createErrorResponse({
        error: protocolError('ERR_SCHEMA', 'Invalid JSON payload', false),
        accountId: this.accountId,
      });
      this.sendToClient(meta, event);
      return;
    }

    const parsed = parseBridgeCommand(parsedJson);
    if (!parsed.ok) {
      this.sendToClient(
        meta,
        createErrorResponse({
          error: parsed.error,
          accountId: this.accountId,
        }),
      );
      return;
    }

    const cmd = parsed.command;
    if (!constantTimeEqual(cmd.token, this.token)) {
      this.sendToClient(
        meta,
        createErrorResponse({
          requestId: cmd.requestId,
          accountId: this.accountId,
          error: protocolError('ERR_AUTH', 'Invalid bridge token', false),
        }),
      );
      return;
    }

    if (cmd.type === 'subscribe_events') {
      if (this.canonicalSubscriber && this.canonicalSubscriber !== meta) {
        this.sendToClient(
          meta,
          createErrorResponse({
            requestId: cmd.requestId,
            accountId: this.accountId,
            error: protocolError('ERR_AUTH', 'Canonical event subscriber already connected', true),
          }),
        );
        return;
      }
      const alreadySubscribed = meta.subscribed;
      meta.subscribed = true;
      this.canonicalSubscriber = meta;
      this.sendToClient(
        meta,
        createOkResponse({
          requestId: cmd.requestId,
          accountId: this.accountId,
          result: { subscribed: true },
        }),
      );
      if (!alreadySubscribed) await this.replayToClient(meta);
      return;
    }

    if (cmd.type === 'ack_event') {
      const { eventId } = parseAckEventPayload(cmd.payload);
      const canAck =
        this.canonicalSubscriber === meta &&
        meta.subscribed &&
        (meta.deliveredEventIds.has(eventId) || meta.acknowledgedEventIds.has(eventId));
      if (!canAck) {
        this.sendToClient(
          meta,
          createErrorResponse({
            requestId: cmd.requestId,
            accountId: this.accountId,
            error: protocolError('ERR_AUTH', 'Event ACK requires current subscriber delivery', false),
          }),
        );
        return;
      }
      const acknowledged = await this.outbox.ack(eventId);
      meta.deliveredEventIds.delete(eventId);
      meta.acknowledgedEventIds.add(eventId);
      this.sendToClient(
        meta,
        createOkResponse({
          requestId: cmd.requestId,
          accountId: this.accountId,
          result: { acknowledged },
        }),
      );
      return;
    }

    if (!this.wa) {
      this.sendToClient(
        meta,
        createErrorResponse({
          requestId: cmd.requestId,
          accountId: this.accountId,
          error: protocolError('ERR_INTERNAL', 'WhatsApp client unavailable', true),
        }),
      );
      return;
    }

    try {
      const result = await this.executeCommand(cmd.type, cmd.payload);
      this.sendToClient(
        meta,
        createOkResponse({
          requestId: cmd.requestId,
          accountId: this.accountId,
          result,
        }),
      );
    } catch (err) {
      const error = asProtocolError(err);
      this.sendToClient(
        meta,
        createErrorResponse({
          requestId: cmd.requestId,
          accountId: this.accountId,
          error,
        }),
      );
    }
  }

  private async executeCommand(
    type: string,
    payload: Record<string, unknown>,
  ): Promise<Record<string, unknown>> {
    if (!this.wa) {
      throw protocolError('ERR_INTERNAL', 'WhatsApp client unavailable', true);
    }

    if (type === 'send_text') {
      const parsed = parseSendTextPayload(payload);
      const sent = await this.wa.sendText(
        parsed.to,
        parsed.text,
        parsed.replyToMessageId,
        parsed.mentions,
        parsed.clientMessageId,
      );
      return { sent };
    }

    if (type === 'send_media') {
      const parsed = parseSendMediaPayload(payload);
      const sent = await this.wa.sendMedia(parsed);
      return { sent };
    }

    if (type === 'send_poll') {
      const parsed = parseSendPollPayload(payload);
      const sent = await this.wa.sendPoll(parsed);
      return { sent };
    }

    if (type === 'delete_message') {
      const parsed = parseDeleteMessagePayload(payload);
      const deleted = await this.wa.deleteMessage(parsed);
      return { deleted };
    }

    if (type === 'react') {
      const parsed = parseReactPayload(payload);
      const reacted = await this.wa.react(parsed);
      return { reacted };
    }

    if (type === 'presence_update') {
      const parsed = parsePresenceUpdatePayload(payload);
      const presence = await this.wa.updatePresence(parsed);
      return { presence };
    }

    if (type === 'lookup_message') {
      const chatJid = String(payload.chatJid || '').trim();
      const messageId = String(payload.messageId || '').trim();
      if (!chatJid || !messageId) {
        return { status: 'unsupported' as const };
      }
      return this.wa.lookupMessage({ chatJid, messageId });
    }

    if (type === 'list_groups') {
      const parsed = parseListGroupsPayload(payload);
      const groups = await this.wa.listGroups(parsed.ids);
      return {
        groups: groups.map((g) => ({
          chatJid: g.normalizedId || g.id,
          id: g.id,
          subject: g.subject,
          subjectOwner: g.subjectOwner,
          subjectTime: g.subjectTime,
          desc: g.desc,
          descOwner: g.descOwner,
          descTime: g.descTime,
          descId: g.descId,
          creation: g.creation,
          owner: g.owner,
          size: g.size,
          participants: g.participants,
          isCommunity: g.isCommunity,
          isParentGroup: g.isParentGroup,
          isAnnounceGrpRestrict: g.isAnnounceGrpRestrict,
          isMemberGroup: g.isMemberGroup,
          restrict: g.restrict,
          announce: g.announce,
          ephemeralDuration: g.ephemeralDuration,
          ephemeralSettingTimestamp: g.ephemeralSettingTimestamp,
          inviteCode: g.inviteCode,
          defaultInviteExpiration: g.defaultInviteExpiration,
          inviteLinkPreventJoin: g.inviteLinkPreventJoin,
          participantAdInfo: g.participantAdInfo,
          groupSet: g.groupSet,
          groupTypes: g.groupTypes,
          linkedParent: g.linkedParent,
          groupMetadata: g.groupMetadata,
        })),
      };
    }

    if (type === 'login_start') {
      const parsed = parseLoginStartPayload(payload);
      const login = await this.wa.loginStart(parsed);
      return { login };
    }

    if (type === 'login_wait') {
      const parsed = parseLoginWaitPayload(payload);
      const login = await this.wa.loginWait(parsed);
      return { login };
    }

    if (type === 'logout') {
      const logout = await this.wa.logout();
      return { logout };
    }

    if (type === 'health') {
      const waHealth = this.wa.health();
      const totals = Array.from(this.clients).reduce(
        (acc, client) => {
          acc.clients += 1;
          acc.inflight += client.inflight;
          acc.dropped += client.droppedEvents;
          return acc;
        },
        { clients: 0, inflight: 0, dropped: 0 },
      );
      // The health request itself is counted as inflight — subtract it
      totals.inflight = Math.max(0, totals.inflight - 1);
      return {
        version: PROTOCOL_VERSION,
        protocolVersion: PROTOCOL_VERSION,
        bridgeVersion: this.bridgeVersion,
        buildId: this.buildId,
        accountId: this.accountId,
        whatsapp: waHealth,
        queue: totals,
        dedupe: {
          droppedInboundDuplicates: waHealth.droppedInboundDuplicates,
          dedupeCacheSize: waHealth.dedupeCacheSize,
        },
        outbox: this.outbox.diagnostics(),
        intakeStopped: this.intakeStopped,
        persistenceFailure: this.persistenceFailure,
      };
    }

    throw protocolError('ERR_UNSUPPORTED', `Unsupported command: ${type}`, false);
  }

  private async broadcastMessage(msg: InboundMessageV2): Promise<void> {
    await this.broadcastReplayable(
      createEventEnvelope({
        type: 'message',
        accountId: this.accountId,
        payload: {
          messageId: msg.messageId,
          chatJid: msg.chatJid,
          participantJid: msg.participantJid,
          senderId: msg.senderId,
          senderPhoneJid: msg.senderPhoneJid,
          lidConflict: msg.lidConflict,
          senderName: msg.senderName,
          isGroup: msg.isGroup,
          text: msg.text,
          timestamp: msg.timestamp,
          mentionedJids: msg.mentionedJids,
          mentionedBot: msg.mentionedBot,
          replyToBot: msg.replyToBot,
          replyToMessageId: msg.replyToMessageId,
          replyToParticipantJid: msg.replyToParticipantJid,
          replyToText: msg.replyToText,
          replyToMedia: mediaMetadata(msg.replyToMedia),
          media: mediaMetadata(msg.media),
        },
      }),
    );
  }

  private sendToClient(meta: ClientMeta, event: BridgeEventEnvelope): boolean {
    if (meta.ws.readyState !== WebSocket.OPEN) return false;
    if (meta.ws.bufferedAmount > MAX_BUFFERED_BYTES) {
      meta.droppedEvents += 1;
      return false;
    }
    meta.ws.send(JSON.stringify(event));
    return true;
  }

  private broadcastEvent(event: BridgeEventEnvelope): void {
    for (const meta of this.clients) {
      this.sendToClient(meta, event);
    }
  }

  private async broadcastReplayable(event: BridgeEventEnvelope): Promise<void> {
    if (this.persistenceFailure) throw new Error('Bridge event intake is stopped');
    let persisted: ReplayableBridgeEvent;
    try {
      persisted = await this.outbox.append(event);
    } catch (error) {
      this.recordPersistenceFailure();
      throw error;
    }

    for (const meta of this.clients) {
      if (!meta.subscribed || this.canonicalSubscriber !== meta) continue;
      if (meta.replaying) {
        meta.replayQueue.push(persisted);
      } else {
        this.deliverReplayable(meta, persisted);
      }
    }
  }

  private async replayToClient(meta: ClientMeta): Promise<void> {
    meta.replaying = true;
    const sent = new Set<string>();
    try {
      for (const event of await this.outbox.pending()) {
        if (!meta.subscribed || !this.clients.has(meta)) return;
        if (this.deliverReplayable(meta, event)) sent.add(event.eventId);
      }
    } finally {
      meta.replaying = false;
      const queued = meta.replayQueue.splice(0);
      if (!meta.subscribed || !this.clients.has(meta)) return;
      for (const event of queued) {
        if (!sent.has(event.eventId)) this.deliverReplayable(meta, event);
      }
    }
  }

  private deliverReplayable(meta: ClientMeta, event: ReplayableBridgeEvent): boolean {
    const delivered = this.sendToClient(meta, event);
    if (delivered) meta.deliveredEventIds.add(event.eventId);
    return delivered;
  }

  private recordPersistenceFailure(): void {
    if (this.persistenceFailure) return;
    this.persistenceFailure = true;
    this.intakeStopped = true;
    this.wa?.stopIntake();
    console.error('Bridge event persistence failed; provider intake stopped');
  }

  private trackProviderEvent(operation: Promise<void>): Promise<void> {
    let tracked: Promise<void>;
    tracked = operation
      .catch((error: unknown) => {
        if (!this.stopping) this.recordPersistenceFailure();
        throw error;
      })
      .finally(() => this.inFlight.delete(tracked));
    this.inFlight.add(tracked);
    return tracked;
  }

  private handleClientClose(meta: ClientMeta): void {
    if (this.canonicalSubscriber === meta) this.canonicalSubscriber = null;
    meta.subscribed = false;
    meta.replaying = false;
    meta.replayQueue.length = 0;
    meta.deliveredEventIds.clear();
    meta.acknowledgedEventIds.clear();
    this.clients.delete(meta);
  }

  diagnostics(): {
    intakeStopped: boolean;
    persistenceFailure: boolean;
    canonicalSubscriber: boolean;
    outbox: ReturnType<BridgeOutbox['diagnostics']>;
  } {
    return {
      intakeStopped: this.intakeStopped,
      persistenceFailure: this.persistenceFailure,
      canonicalSubscriber: this.canonicalSubscriber !== null,
      outbox: this.outbox.diagnostics(),
    };
  }

  async stop(): Promise<void> {
    this.stopping = true;
    this.intakeStopped = true;
    if (this.wa) {
      await this.wa.stop();
      this.wa = null;
    }
    await Promise.allSettled(Array.from(this.inFlight));
    await this.outbox.flush();
    this.canonicalSubscriber = null;
    for (const meta of this.clients) {
      meta.ws.close();
    }
    this.clients.clear();

    if (this.wss) {
      this.wss.close();
      this.wss = null;
    }
  }
}
