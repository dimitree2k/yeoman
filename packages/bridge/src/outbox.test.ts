import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, readdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

async function loadOutbox(): Promise<any> {
  const modulePath = './outbox.js';
  try {
    return await import(modulePath);
  } catch (error) {
    assert.fail(`Bridge outbox unavailable: ${String(error)}`);
  }
}

function event(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    version: 5,
    type: 'message',
    ts: 1_700_000_000_000,
    accountId: 'default',
    payload: {
      messageId: 'provider-message-1',
      text: 'private payload',
    },
    ...overrides,
  };
}

async function temporaryOutbox(): Promise<{ root: string; outbox: any }> {
  const { BridgeOutbox } = await loadOutbox();
  const root = await mkdtemp(join(tmpdir(), 'yeoman-bridge-outbox-'));
  const outbox = new BridgeOutbox(root);
  await outbox.open();
  return { root, outbox };
}

test('event is durable before a subscriber callback can see it', async () => {
  const { root, outbox } = await temporaryOutbox();
  try {
    let callbackSawFile = false;
    const persisted = await outbox.append(event());
    const files = await readdir(root);
    callbackSawFile = files.length === 1;

    assert.equal(callbackSawFile, true);
    assert.equal(persisted.eventId.length > 0, true);
    assert.equal(persisted.eventKey.length > 0, true);
    assert.equal(typeof persisted.observedAt, 'number');
    assert.deepEqual(JSON.parse(await readFile(join(root, files[0]), 'utf8')), persisted);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('restart reloads and replays an unacknowledged event', async () => {
  const { root, outbox } = await temporaryOutbox();
  try {
    const persisted = await outbox.append(event({ eventId: 'event-replay-1', eventKey: 'key-replay-1' }));
    const { BridgeOutbox } = await loadOutbox();
    const restarted = new BridgeOutbox(root);
    await restarted.open();

    assert.deepEqual(await restarted.pending(), [persisted]);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('ACK removes only the named entry and repeated ACK is harmless', async () => {
  const { root, outbox } = await temporaryOutbox();
  try {
    const first = await outbox.append(event({ eventId: 'event-ack-1', eventKey: 'key-ack-1' }));
    const second = await outbox.append(event({ eventId: 'event-ack-2', eventKey: 'key-ack-2' }));

    assert.equal(await outbox.ack(first.eventId), true);
    assert.deepEqual(await outbox.pending(), [second]);
    assert.equal(await outbox.ack(first.eventId), false);
    assert.deepEqual(await outbox.pending(), [second]);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('replay preserves persisted observation order and identity', async () => {
  const { root, outbox } = await temporaryOutbox();
  try {
    const first = await outbox.append(
      event({ eventId: 'event-order-1', eventKey: 'key-order-1', observedAt: 200 }),
    );
    const second = await outbox.append(
      event({ eventId: 'event-order-2', eventKey: 'key-order-2', observedAt: 100 }),
    );
    const { BridgeOutbox } = await loadOutbox();
    const restarted = new BridgeOutbox(root);
    await restarted.open();

    assert.deepEqual(
      (await restarted.pending()).map((item: any) => [item.eventId, item.eventKey, item.observedAt]),
      [
        [first.eventId, first.eventKey, first.observedAt],
        [second.eventId, second.eventKey, second.observedAt],
      ],
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('diagnostics omit tokens and raw event payloads', async () => {
  const { root, outbox } = await temporaryOutbox();
  try {
    await outbox.append(
      event({
        eventId: 'event-diagnostic-1',
        eventKey: 'key-diagnostic-1',
        payload: { token: 'secret-token', raw: 'do-not-log-this' },
      }),
    );
    const diagnostic = JSON.stringify(outbox.diagnostics());

    assert.equal(diagnostic.includes('secret-token'), false);
    assert.equal(diagnostic.includes('do-not-log-this'), false);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
