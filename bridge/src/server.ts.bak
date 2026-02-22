import { timingSafeEqual } from 'crypto';

import { WebSocketServer, WebSocket, type RawData } from 'ws';

import {
  asProtocolError,
  createErrorResponse,
  createEventEnvelope,
  createOkResponse,
  isLoopbackAddress,
  parseBridgeCommand,
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
import { WhatsAppClient, type InboundMessageV2 } from './whatsapp.js';

type ClientMeta = {
  ws: WebSocket;
  inflight: number;
  droppedEvents: number;
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

export class BridgeServer {
  private wss: WebSocketServer | null = null;
  private wa: WhatsAppClient | null = null;
  private readonly clients = new Set<ClientMeta>();

  constructor(
    private readonly host: string,
    private readonly port: number,
    private readonly authDir: string,
    private readonly mediaIncomingDir: string,
    private readonly mediaOutgoingDir: string,
    private readonly persistInboundAudio: boolean,
    private readonly acceptFromMe: boolean,
    private readonly token: string,
    private readonly bridgeVersion: string,
    private readonly buildId: string,
    private readonly readReceipts: boolean,
    private readonly accountId = 'default',
  ) {}

  async start(): Promise<void> {
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
      acceptFromMe: this.acceptFromMe,
      readReceipts: this.readReceipts,
      accountId: this.accountId,
      onMessage: (msg) => this.broadcastMessage(msg),
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

      const meta: ClientMeta = { ws, inflight: 0, droppedEvents: 0 };
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
        this.clients.delete(meta);
      });

      ws.on('error', () => {
        this.clients.delete(meta);
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
      const sent = await this.wa.sendText(parsed.to, parsed.text, parsed.replyToMessageId);
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

    if (type === 'list_groups') {
      const parsed = parseListGroupsPayload(payload);
      const groups = await this.wa.listGroups(parsed.ids);
      return {
        groups: groups.map((g) => ({ chatJid: g.id, subject: g.subject })),
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
      };
    }

    throw protocolError('ERR_UNSUPPORTED', `Unsupported command: ${type}`, false);
  }

  private broadcastMessage(msg: InboundMessageV2): void {
    this.broadcastEvent(
      createEventEnvelope({
        type: 'message',
        accountId: this.accountId,
        payload: {
          messageId: msg.messageId,
          chatJid: msg.chatJid,
          participantJid: msg.participantJid,
          senderId: msg.senderId,
          isGroup: msg.isGroup,
          text: msg.text,
          timestamp: msg.timestamp,
          mentionedJids: msg.mentionedJids,
          mentionedBot: msg.mentionedBot,
          replyToBot: msg.replyToBot,
          replyToMessageId: msg.replyToMessageId,
          replyToParticipantJid: msg.replyToParticipantJid,
          replyToText: msg.replyToText,
          media: msg.media,
        },
      }),
    );
  }

  private sendToClient(meta: ClientMeta, event: BridgeEventEnvelope): void {
    if (meta.ws.readyState !== WebSocket.OPEN) return;
    if (meta.ws.bufferedAmount > MAX_BUFFERED_BYTES) {
      meta.droppedEvents += 1;
      return;
    }
    meta.ws.send(JSON.stringify(event));
  }

  private broadcastEvent(event: BridgeEventEnvelope): void {
    for (const meta of this.clients) {
      this.sendToClient(meta, event);
    }
  }

  async stop(): Promise<void> {
    for (const meta of this.clients) {
      meta.ws.close();
    }
    this.clients.clear();

    if (this.wss) {
      this.wss.close();
      this.wss = null;
    }

    if (this.wa) {
      await this.wa.stop();
      this.wa = null;
    }
  }
}
