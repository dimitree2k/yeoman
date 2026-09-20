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
