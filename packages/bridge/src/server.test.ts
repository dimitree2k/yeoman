import test from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync } from 'node:fs';
import { mkdtemp, readFile, readdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { BridgeServer } from './server.js';
import { MAX_BRIDGE_FRAME_BYTES, PROTOCOL_VERSION, createEventEnvelope } from './protocol.js';

function fakeClient() {
  const messages: unknown[] = [];
  const ws = {
    readyState: 1,
    bufferedAmount: 0,
    send: (raw: string) => messages.push(JSON.parse(raw)),
    close: () => undefined,
  };
  return { ws, messages };
}

function clientMeta(ws: any, subscribed = false) {
  return {
    ws,
    inflight: 0,
    droppedEvents: 0,
    subscribed,
    replaying: false,
    replayQueue: [],
    deliveredEventIds: new Set<string>(),
    acknowledgedEventIds: new Set<string>(),
  };
}

function makeServer(outboxDir: string): BridgeServer {
  return new BridgeServer(
    '127.0.0.1',
    0,
    '',
    '',
    '',
    false,
    false,
    false,
    'secret',
    '0.2.0',
    'test-build',
    true,
    'default',
    outboxDir,
  );
}

function eventWithTextThatFitsSerializedLimit(textBytes: number): Record<string, unknown> {
  const event = {
    version: PROTOCOL_VERSION,
    type: 'message',
    ts: 1_700_000_000_000,
    observedAt: 1_700_000_000_000,
    accountId: 'default',
    eventId: 'boundary-event-1',
    eventKey: 'boundary-key-1',
    payload: { messageId: 'boundary-message-1', text: '' },
  };
  const overhead = Buffer.byteLength(JSON.stringify(event), 'utf8');
  let text = 'é'.repeat(Math.floor(Math.max(0, textBytes - overhead) / 2));
  while (Buffer.byteLength(text, 'utf8') < textBytes - overhead) text += 'x';
  while (Buffer.byteLength(text, 'utf8') > textBytes - overhead) text = text.slice(0, -1);
  event.payload.text = text;
  assert.equal(Buffer.byteLength(JSON.stringify(event), 'utf8'), textBytes);
  return event;
}

test('BridgeServer dispatches delete_message to the WhatsApp client', async () => {
  const server = new BridgeServer(
    '127.0.0.1',
    0,
    '',
    '',
    '',
    false,
    false,
    false,
    'secret',
    '0.2.0',
    'test-build',
    true,
  );
  const calls: unknown[] = [];
  const deleted = {
    chatJid: '12345@s.whatsapp.net',
    messageId: 'BAE5EXACTMESSAGEID',
  };

  (server as any).wa = {
    deleteMessage: async (payload: unknown) => {
      calls.push(payload);
      return deleted;
    },
  };

  const result = await (server as any).executeCommand('delete_message', deleted);

  assert.deepEqual(result, { deleted });
  assert.deepEqual(calls, [deleted]);
});

test('BridgeServer dispatches forward_message with exact source identities', async () => {
  const server = new BridgeServer(
    '127.0.0.1',
    0,
    '',
    '',
    '',
    false,
    false,
    false,
    'secret',
    '0.2.0',
    'test-build',
    true,
  );
  const calls: unknown[] = [];
  const forwarded = { to: 'target@g.us', messageId: 'OUT-1' };
  (server as any).wa = {
    forwardMessage: async (payload: unknown) => {
      calls.push(payload);
      return forwarded;
    },
  };

  const result = await (server as any).executeCommand('forward_message', {
    to: 'target@g.us',
    sourceChatJid: 'source@g.us',
    sourceMessageId: 'SRC-1',
  });

  assert.deepEqual(result, { forwarded });
  assert.deepEqual(calls, [{
    to: 'target@g.us',
    sourceChatJid: 'source@g.us',
    sourceMessageId: 'SRC-1',
    clientMessageId: undefined,
  }]);
});

test('BridgeServer sends replayable events only after authenticated subscription', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-bridge-server-'));
  try {
    const server = makeServer(root);
    const unauthenticated = fakeClient();
    const subscriber = fakeClient();
    const unauthMeta = clientMeta(unauthenticated.ws);
    const subscriberMeta = clientMeta(subscriber.ws);
    (server as any).clients.add(unauthMeta);
    (server as any).clients.add(subscriberMeta);

    await (server as any).broadcastReplayable(
      createEventEnvelope({
        type: 'message',
        payload: { messageId: 'private-1', text: 'private payload' },
      }),
    );
    assert.equal(unauthenticated.messages.length, 0);

    await (server as any).handleClientMessage(
      subscriberMeta,
      JSON.stringify({
        version: PROTOCOL_VERSION,
        type: 'subscribe_events',
        token: 'secret',
        requestId: 'subscribe-1',
        payload: {},
      }),
    );
    assert.equal(subscriber.messages.length, 2);
    assert.equal((subscriber.messages[1] as any).payload.text, 'private payload');
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('BridgeServer persists before a subscriber send and preserves pending events on slow clients', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-bridge-server-'));
  try {
    const server = makeServer(root);
    const subscriber = fakeClient();
    const meta = clientMeta(subscriber.ws, true);
    (server as any).clients.add(meta);
    (server as any).canonicalSubscriber = meta;
    const deliveredAfterPersist: boolean[] = [];
    subscriber.ws.send = () => {
      deliveredAfterPersist.push(readdirSync(root).length > 0);
      return 0;
    };

    await (server as any).broadcastReplayable(
      createEventEnvelope({ type: 'message', payload: { messageId: 'durable-1' } }),
    );
    assert.deepEqual(deliveredAfterPersist, [true]);

    subscriber.ws.bufferedAmount = 3 * 1024 * 1024;
    await (server as any).broadcastReplayable(
      createEventEnvelope({ type: 'message', payload: { messageId: 'slow-1' } }),
    );
    assert.equal((await (server as any).outbox.pending()).length, 2);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('BridgeServer replays complete message metadata without binary payloads', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-bridge-message-payload-'));
  try {
    const server = makeServer(root);
    const message = {
      messageId: 'media-message-1',
      chatJid: 'chat@g.us',
      participantJid: '4915@s.whatsapp.net',
      senderId: '4915',
      isGroup: true,
      text: '[Document] report',
      timestamp: 1_700_000_000,
      mentionedJids: [],
      mentionedBot: false,
      replyToBot: false,
      replyToMedia: {
        kind: 'document',
        mimeType: 'application/pdf',
        fileName: 'quoted.pdf',
        bytes: 128,
        path: '/safe/media/quoted.pdf',
        sha256: 'a'.repeat(64),
      },
      media: {
        kind: 'document',
        mimeType: 'application/pdf',
        fileName: 'report.pdf',
        bytes: 256,
        path: '/safe/media/report.pdf',
        sha256: 'b'.repeat(64),
      },
    };

    await (server as any).broadcastMessage(message);
    const restarted = makeServer(root);
    await (restarted as any).outbox.open();
    const [event] = await (restarted as any).outbox.pending();

    assert.deepEqual(event.payload.media, message.media);
    assert.deepEqual(event.payload.replyToMedia, message.replyToMedia);
    assert.equal('data' in event.payload.media, false);
    assert.equal('base64' in event.payload.media, false);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('BridgeServer derives provider identity independently of content and quarantines conflicts', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-bridge-provider-identity-'));
  try {
    const message = {
      messageId: 'provider-message-1',
      chatJid: 'chat@g.us',
      participantJid: '4915@s.whatsapp.net',
      senderId: '4915',
      isGroup: true,
      text: 'first provider text',
      timestamp: 1_700_000_000,
      mentionedJids: [],
      mentionedBot: false,
      replyToBot: false,
    };
    const server = makeServer(root);
    await (server as any).broadcastMessage(message);
    const first = (await (server as any).outbox.pending())[0];
    assert.ok(first);

    const replay = makeServer(root);
    await (replay as any).broadcastMessage(message);
    const replayed = await (replay as any).outbox.pending();
    assert.equal(replayed.length, 1);
    assert.equal(replayed[0].eventId, first.eventId);
    assert.equal(replayed[0].eventKey, first.eventKey);

    await assert.rejects(
      (replay as any).broadcastMessage({ ...message, text: 'conflicting provider text' }),
      /Conflicting bridge outbox event/,
    );
    assert.equal((await (replay as any).outbox.pending()).length, 1);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('BridgeServer accepts an event exactly at the UTF-8 serialized byte ceiling', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-bridge-event-limit-'));
  try {
    const server = makeServer(root);
    const client = fakeClient();
    const meta = clientMeta(client.ws, true);
    (server as any).clients.add(meta);
    (server as any).canonicalSubscriber = meta;
    const event = eventWithTextThatFitsSerializedLimit(MAX_BRIDGE_FRAME_BYTES);

    await (server as any).broadcastReplayable(event);

    assert.equal((await (server as any).outbox.pending()).length, 1);
    assert.equal(client.messages.length, 1);
    assert.equal((server as any).intakeStopped, false);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('BridgeServer quarantines oversized text with a durable identity-only diagnostic', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-bridge-event-limit-'));
  try {
    const server = makeServer(root);
    const client = fakeClient();
    const meta = clientMeta(client.ws, true);
    (server as any).clients.add(meta);
    (server as any).canonicalSubscriber = meta;
    const event = eventWithTextThatFitsSerializedLimit(MAX_BRIDGE_FRAME_BYTES);
    (event.payload as any).text += 'x';

    await assert.rejects(
      (server as any).broadcastReplayable(event),
      /serialized event exceeds 262144 bytes/,
    );

    assert.deepEqual(await (server as any).outbox.pending(), []);
    assert.equal(client.messages.length, 0);
    assert.equal((server as any).intakeStopped, true);
    assert.equal((server as any).persistenceFailure, true);
    assert.equal((server as any).diagnostics().outbox.rejected, 1);
    const files = await readdir(join(root, 'quarantine'));
    assert.equal(files.length, 1);
    const diagnostic = JSON.parse(await readFile(join(root, 'quarantine', files[0]), 'utf8'));
    assert.deepEqual(Object.keys(diagnostic).sort(), [
      'eventId', 'eventKey', 'observedAt', 'reason', 'serializedBytes', 'type',
    ]);
    assert.match(diagnostic.eventId, /^sha256:[0-9a-f]{64}$/);
    assert.match(diagnostic.eventKey, /^sha256:[0-9a-f]{64}$/);
    assert.equal(diagnostic.type, 'message');
    assert.equal(diagnostic.serializedBytes, MAX_BRIDGE_FRAME_BYTES + 1);
    assert.equal(diagnostic.reason, 'serialized-size-limit');
    assert.equal(JSON.stringify(diagnostic).includes('é'), false);
    assert.equal(JSON.stringify(diagnostic).includes('xxx'), false);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('BridgeServer bounds oversized response, status, and error frames with a safe fallback', () => {
  const server = makeServer('/tmp/unused-bridge-frame-limit');
  const client = fakeClient();
  const meta = clientMeta(client.ws, true);
  const giant = 'provider detail '.repeat(30_000);
  const giantRequestId = 'request-id-'.repeat(30_000);

  for (const event of [
    createEventEnvelope({
      type: 'response',
      requestId: giantRequestId,
      payload: { result: { providerDetail: giant } },
    }),
    createEventEnvelope({ type: 'status', payload: { detail: giant } }),
    createEventEnvelope({
      type: 'error',
      payload: { error: { code: 'ERR_INTERNAL', message: giant, retryable: true } },
    }),
  ]) {
    assert.equal((server as any).sendToClient(meta, event), true);
    const raw = JSON.stringify(client.messages.at(-1));
    assert.ok(Buffer.byteLength(raw, 'utf8') <= MAX_BRIDGE_FRAME_BYTES);
    assert.equal(raw.includes(giant), false);
    assert.equal(raw.includes(giantRequestId), false);
    assert.equal((client.messages.at(-1) as any).type, 'response');
    assert.equal((client.messages.at(-1) as any).payload.error.code, 'ERR_PAYLOAD_TOO_LARGE');
  }

  const safeRequest = createEventEnvelope({
    type: 'response',
    requestId: 'safe-request',
    payload: { result: { providerDetail: giant } },
  });
  assert.equal((server as any).sendToClient(meta, safeRequest), true);
  assert.equal((client.messages.at(-1) as any).requestId, 'safe-request');
});

test('BridgeServer quarantines oversized media metadata without a pending event or payload leak', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-bridge-event-limit-'));
  try {
    const server = makeServer(root);
    const oversizedHash = 'media-hash-'.repeat(30_000);
    await assert.rejects(
      (server as any).broadcastMessage({
        messageId: 'oversized-media-1',
        chatJid: 'chat@g.us',
        participantJid: '4915@s.whatsapp.net',
        senderId: '4915',
        isGroup: true,
        text: '[Document]',
        timestamp: 1_700_000_000,
        mentionedJids: [],
        mentionedBot: false,
        replyToBot: false,
        media: {
          kind: 'document',
          mimeType: 'application/pdf',
          fileName: 'large.pdf',
          bytes: 1,
          sha256: oversizedHash,
        },
      }),
      /serialized event exceeds 262144 bytes/,
    );

    assert.deepEqual(await (server as any).outbox.pending(), []);
    const files = await readdir(join(root, 'quarantine'));
    assert.equal(files.length, 1);
    const diagnostic = await readFile(join(root, 'quarantine', files[0]), 'utf8');
    assert.equal(diagnostic.includes(oversizedHash), false);
    assert.equal((server as any).intakeStopped, true);
    assert.equal((server as any).persistenceFailure, true);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('BridgeServer bounds hostile rejection identities to deterministic digests', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-bridge-event-limit-'));
  try {
    const server = makeServer(root);
    const event = {
      version: PROTOCOL_VERSION,
      type: 'message',
      ts: 1_700_000_000_000,
      observedAt: 1_700_000_000_000,
      accountId: 'default',
      eventId: '../../event\u0000'.repeat(30_000),
      eventKey: '../../key\u0000'.repeat(30_000),
      payload: { messageId: 'hostile-identity-1', text: 'x'.repeat(MAX_BRIDGE_FRAME_BYTES) },
    };

    await assert.rejects(
      (server as any).broadcastReplayable(event),
      /serialized event exceeds 262144 bytes/,
    );

    const files = (await readdir(join(root, 'quarantine'))).filter((name) =>
      name.startsWith('rejected-'),
    );
    assert.equal(files.length, 1);
    const diagnostic = await readFile(join(root, 'quarantine', files[0]), 'utf8');
    assert.ok(Buffer.byteLength(diagnostic, 'utf8') <= MAX_BRIDGE_FRAME_BYTES);
    assert.equal(diagnostic.includes('../../'), false);
    assert.equal(diagnostic.includes('\u0000'), false);
    const parsed = JSON.parse(diagnostic);
    assert.match(parsed.eventId, /^sha256:[0-9a-f]{64}$/);
    assert.match(parsed.eventKey, /^sha256:[0-9a-f]{64}$/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('status events remain operational broadcasts and are not outbox business events', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-bridge-server-'));
  try {
    const server = makeServer(root);
    const client = fakeClient();
    const meta = clientMeta(client.ws);
    (server as any).clients.add(meta);

    (server as any).broadcastEvent(
      createEventEnvelope({ type: 'status', payload: { status: 'connected' } }),
    );
    assert.equal(client.messages.length, 1);
    assert.deepEqual(await (server as any).outbox.pending(), []);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('only the canonical subscriber can receive and ACK replayable events', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-bridge-server-'));
  try {
    const server = makeServer(root);
    const first = fakeClient();
    const second = fakeClient();
    const firstMeta = clientMeta(first.ws);
    const secondMeta = clientMeta(second.ws);
    (server as any).clients.add(firstMeta);
    (server as any).clients.add(secondMeta);
    const event = await (server as any).outbox.append(
      createEventEnvelope({ type: 'message', payload: { messageId: 'ack-boundary-1' } }),
    );

    await (server as any).handleClientMessage(
      firstMeta,
      JSON.stringify({ version: PROTOCOL_VERSION, type: 'subscribe_events', token: 'secret', payload: {} }),
    );
    await (server as any).handleClientMessage(
      secondMeta,
      JSON.stringify({ version: PROTOCOL_VERSION, type: 'subscribe_events', token: 'secret', payload: {} }),
    );
    assert.equal(firstMeta.subscribed, true);
    assert.equal(secondMeta.subscribed, false);
    assert.equal((second.messages[0] as any).payload.ok, false);

    await (server as any).handleClientMessage(
      secondMeta,
      JSON.stringify({
        version: PROTOCOL_VERSION,
        type: 'ack_event',
        token: 'secret',
        payload: { eventId: event.eventId },
      }),
    );
    assert.equal((second.messages.at(-1) as any).payload.ok, false);
    assert.equal((await (server as any).outbox.pending()).length, 1);

    await (server as any).handleClientMessage(
      firstMeta,
      JSON.stringify({
        version: PROTOCOL_VERSION,
        type: 'ack_event',
        token: 'secret',
        payload: { eventId: event.eventId },
      }),
    );
    assert.deepEqual(await (server as any).outbox.pending(), []);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('disconnect releases the canonical slot and leaves pending events for replay', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-bridge-server-'));
  try {
    const server = makeServer(root);
    const first = fakeClient();
    const firstMeta = clientMeta(first.ws);
    (server as any).clients.add(firstMeta);
    await (server as any).outbox.append(
      createEventEnvelope({ type: 'message', payload: { messageId: 'disconnect-replay-1' } }),
    );
    await (server as any).handleClientMessage(
      firstMeta,
      JSON.stringify({ version: PROTOCOL_VERSION, type: 'subscribe_events', token: 'secret', payload: {} }),
    );
    (server as any).handleClientClose(firstMeta);
    assert.equal(firstMeta.subscribed, false);

    const second = fakeClient();
    const secondMeta = clientMeta(second.ws);
    (server as any).clients.add(secondMeta);
    await (server as any).handleClientMessage(
      secondMeta,
      JSON.stringify({ version: PROTOCOL_VERSION, type: 'subscribe_events', token: 'secret', payload: {} }),
    );
    assert.equal(second.messages.some((item: any) => item.payload?.messageId === 'disconnect-replay-1'), true);
    assert.equal((await (server as any).outbox.pending()).length, 1);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('persistence failure is visible and stops provider intake', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-bridge-server-'));
  try {
    const server = makeServer(root);
    (server as any).outbox.append = async () => {
      throw new Error('disk full with payload secret-token');
    };

    await assert.rejects(
      (server as any).broadcastReplayable(
        createEventEnvelope({ type: 'message', payload: { text: 'raw payload' } }),
      ),
      /disk full/,
    );
    assert.equal((server as any).intakeStopped, true);
    assert.equal((server as any).persistenceFailure, true);
    assert.equal(JSON.stringify((server as any).diagnostics()).includes('secret-token'), false);
    assert.equal(JSON.stringify((server as any).diagnostics()).includes('raw payload'), false);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('stop drains an in-flight persistence operation before returning', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-bridge-server-'));
  try {
    const server = makeServer(root);
    let release!: () => void;
    const blocked = new Promise<void>((resolve) => {
      release = resolve;
    });
    (server as any).outbox.append = async () => {
      await blocked;
      return { eventId: 'drained-1', eventKey: 'drained-key', observedAt: 1 };
    };
    (server as any).trackProviderEvent(
      (server as any).broadcastReplayable(
        createEventEnvelope({ type: 'message', payload: { messageId: 'drained-1' } }),
      ),
    );
    let stopped = false;
    const stopping = server.stop().then(() => {
      stopped = true;
    });
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(stopped, false);
    release();
    await stopping;
    assert.equal(stopped, true);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
