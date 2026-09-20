import test from 'node:test';
import assert from 'node:assert/strict';
import { chmod, lstat, mkdir, mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises';
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
    observedAt: 1_700_000_000_000,
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
    const files = (await readdir(root)).filter((file) => file.endsWith('.json'));
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

test('outbox rejects every non-replayable event type', async () => {
  const { root, outbox } = await temporaryOutbox();
  try {
    for (const type of ['status', 'qr', 'error', 'response']) {
      await assert.rejects(
        outbox.append(event({ type })),
        /replayable event type/,
      );
    }
    assert.deepEqual(await outbox.pending(), []);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('open quarantines malformed, overflow, and filename-mismatched records', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-bridge-outbox-'));
  try {
    await writeFile(join(root, '00000000000000000001-bad-key.json'), '{not-json', { mode: 0o600 });
    await writeFile(
      join(root, '999999999999999999999999-overflow-key.json'),
      JSON.stringify(event({ eventId: 'overflow-event', eventKey: 'overflow-key' })),
      { mode: 0o600 },
    );
    await writeFile(
      join(root, '00000000000000000002-wrong-id-wrong-key.json'),
      JSON.stringify(event({ eventId: 'actual-id', eventKey: 'actual-key' })),
      { mode: 0o600 },
    );
    await writeFile(
      join(root, '00000000000000000003-status-id-status-key.json'),
      JSON.stringify(event({ type: 'status', eventId: 'status-id', eventKey: 'status-key' })),
      { mode: 0o600 },
    );
    const { BridgeOutbox } = await loadOutbox();
    const outbox = new BridgeOutbox(root);
    await outbox.open();

    assert.deepEqual(await outbox.pending(), []);
    assert.equal((outbox.diagnostics() as any).quarantined, 4);
    const quarantine = await readdir(join(root, 'quarantine'));
    assert.equal(quarantine.length, 4);
    for (const file of quarantine) {
      assert.equal((await lstat(join(root, 'quarantine', file))).mode & 0o777, 0o600);
    }
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('open quarantines duplicate sequence records and keeps order deterministic', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-bridge-outbox-'));
  try {
    const first = event({ eventId: 'duplicate-sequence-1', eventKey: 'duplicate-key-1' });
    const second = event({ eventId: 'duplicate-sequence-2', eventKey: 'duplicate-key-2' });
    await writeFile(
      join(root, '00000000000000000001-duplicate-sequence-1-duplicate-key-1.json'),
      JSON.stringify(first),
      { mode: 0o600 },
    );
    await writeFile(
      join(root, '00000000000000000001-duplicate-sequence-2-duplicate-key-2.json'),
      JSON.stringify(second),
      { mode: 0o600 },
    );
    const { BridgeOutbox } = await loadOutbox();
    const outbox = new BridgeOutbox(root);
    await outbox.open();

    assert.deepEqual((await outbox.pending()).map((item: any) => item.eventId), ['duplicate-sequence-1']);
    assert.equal((outbox.diagnostics() as any).quarantined, 1);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('open enforces owner-only directory and file permissions', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-bridge-outbox-'));
  try {
    await chmod(root, 0o755);
    const file = join(root, '00000000000000000001-safe-event-safe-key.json');
    await writeFile(file, JSON.stringify(event({ eventId: 'safe-event', eventKey: 'safe-key' })), {
      mode: 0o644,
    });
    const { BridgeOutbox } = await loadOutbox();
    const outbox = new BridgeOutbox(root);
    await outbox.open();

    assert.equal((await lstat(root)).mode & 0o777, 0o700);
    assert.equal((await lstat(file)).mode & 0o777, 0o600);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('open enforces owner-only quarantine file permissions', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-bridge-outbox-'));
  try {
    const quarantine = join(root, 'quarantine');
    await mkdir(quarantine, { mode: 0o755 });
    const file = join(quarantine, '2026-09-20-malformed-json.json');
    await writeFile(file, '{private payload}', { mode: 0o644 });
    const { BridgeOutbox } = await loadOutbox();
    const outbox = new BridgeOutbox(root);
    await outbox.open();

    assert.equal((await lstat(quarantine)).mode & 0o777, 0o700);
    assert.equal((await lstat(file)).mode & 0o777, 0o600);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('open fails closed for a non-regular spool candidate', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-bridge-outbox-'));
  try {
    await mkdir(join(root, '00000000000000000001-not-a-file'), { mode: 0o700 });
    const { BridgeOutbox } = await loadOutbox();
    await assert.rejects(new BridgeOutbox(root).open(), /regular|quarantine|outbox/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
