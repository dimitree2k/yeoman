import test from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync } from 'node:fs';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { BridgeServer } from './server.js';
import { PROTOCOL_VERSION, createEventEnvelope } from './protocol.js';

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
