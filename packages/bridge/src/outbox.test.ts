import test from 'node:test';
import assert from 'node:assert/strict';
import { chmod, lstat, mkdir, mkdtemp, open as openFile, readFile, readdir, rename, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { MAX_BRIDGE_FRAME_BYTES } from './protocol.js';

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

async function pathExists(path: string): Promise<boolean> {
  try {
    await lstat(path);
    return true;
  } catch {
    return false;
  }
}

function oversizedEvent(eventId: string, eventKey: string): Record<string, unknown> {
  return event({
    eventId,
    eventKey,
    payload: { messageId: eventId, text: 'x'.repeat(MAX_BRIDGE_FRAME_BYTES) },
  });
}

function eventFileName(eventValue: Record<string, unknown>): string {
  return `00000000000000000001-${encodeURIComponent(String(eventValue.eventId))}-${encodeURIComponent(String(eventValue.eventKey))}.json`;
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

test('reload rejection is durable before unlink for canonical and staged oversized records', async () => {
  for (const staged of [false, true]) {
    const root = await mkdtemp(join(tmpdir(), 'yeoman-bridge-outbox-rejection-order-'));
    try {
      const oversized = oversizedEvent(
        staged ? 'oversized-staged-event' : 'oversized-canonical-event',
        staged ? 'oversized-staged-key' : 'oversized-canonical-key',
      );
      const canonicalName = eventFileName(oversized);
      const recordPath = staged
        ? join(root, `.${canonicalName}.aaaaaaaaaaaaaaaa.tmp`)
        : join(root, canonicalName);
      await writeFile(recordPath, JSON.stringify(oversized), { mode: 0o600 });

      const crashing = new (await loadOutbox()).BridgeOutbox(root);
      (crashing as any).writeRejectionDiagnostic = async () => {
        throw new Error('crash-before-rejection-diagnostic');
      };
      await assert.rejects(crashing.open(), /crash-before-rejection-diagnostic/);
      assert.equal(await pathExists(recordPath), true);

      const recovery = new (await loadOutbox()).BridgeOutbox(root);
      const serializedBytes = Buffer.byteLength(JSON.stringify(oversized), 'utf8');
      await (recovery as any).writeRejectionDiagnostic(
        oversized,
        serializedBytes,
        'serialized-size-limit',
      );
      assert.equal(await pathExists(recordPath), true);

      const restarted = new (await loadOutbox()).BridgeOutbox(root);
      await restarted.open();
      assert.deepEqual(await restarted.pending(), []);
      assert.equal(await pathExists(recordPath), false);
      const rejectionFiles = (await readdir(join(root, 'quarantine'))).filter((name) =>
        name.startsWith('rejected-'),
      );
      assert.equal(rejectionFiles.length, 1);

      const secondRestart = new (await loadOutbox()).BridgeOutbox(root);
      await secondRestart.open();
      assert.equal(
        (await readdir(join(root, 'quarantine'))).filter((name) => name.startsWith('rejected-')).length,
        1,
      );
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  }
});

test('reload ignores crash temps left before fsync or publication without leaking payloads', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-bridge-outbox-stale-rejection-'));
  try {
    const oversized = oversizedEvent('stale-temp-event', 'stale-temp-key');
    const canonicalName = eventFileName(oversized);
    const sourcePath = join(root, canonicalName);
    await writeFile(sourcePath, JSON.stringify(oversized), { mode: 0o600 });
    const quarantine = join(root, 'quarantine');
    await mkdir(quarantine, { mode: 0o700 });

    const seed = new (await loadOutbox()).BridgeOutbox(root);
    const serializedBytes = Buffer.byteLength(JSON.stringify(oversized), 'utf8');
    await (seed as any).writeRejectionDiagnostic(oversized, serializedBytes, 'serialized-size-limit');
    const [finalName] = (await readdir(quarantine)).filter((name) => name.startsWith('rejected-'));
    assert.ok(finalName);
    await rename(join(quarantine, finalName), join(quarantine, `.${finalName}.matching.tmp`));
    await writeFile(join(quarantine, `.${finalName}.empty.tmp`), '', { mode: 0o600 });
    await writeFile(
      join(quarantine, `.${finalName}.partial.tmp`),
      '{"eventId":"stale-temp-event","raw":"stale-temp-payload-secret"}',
      { mode: 0o600 },
    );

    const restarted = new (await loadOutbox()).BridgeOutbox(root);
    await restarted.open();
    assert.deepEqual(await restarted.pending(), []);
    assert.equal(await pathExists(sourcePath), false);
    const recovered = (await readdir(quarantine)).filter((name) => name.startsWith('rejected-'));
    assert.deepEqual(recovered, [finalName]);
    const diagnostic = await readFile(join(quarantine, finalName), 'utf8');
    assert.equal(diagnostic.includes('stale-temp-event'), false);
    assert.equal(diagnostic.includes('stale-temp-key'), false);
    assert.equal(diagnostic.includes('stale-temp-payload-secret'), false);
    assert.deepEqual(
      (await readdir(quarantine)).filter((name) => name.endsWith('.tmp')).sort(),
      [`.${finalName}.empty.tmp`, `.${finalName}.matching.tmp`, `.${finalName}.partial.tmp`].sort(),
    );

    const secondRestart = new (await loadOutbox()).BridgeOutbox(root);
    await secondRestart.open();
    assert.deepEqual(
      (await readdir(quarantine)).filter((name) => name.startsWith('rejected-')),
      [finalName],
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('a held writer temp survives another rejection writer', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-bridge-outbox-live-rejection-temp-'));
  let liveHandle: Awaited<ReturnType<typeof openFile>> | undefined;
  try {
    const quarantine = join(root, 'quarantine');
    await mkdir(quarantine, { mode: 0o700 });
    const oversized = oversizedEvent('live-temp-event', 'live-temp-key');
    const serializedBytes = Buffer.byteLength(JSON.stringify(oversized), 'utf8');
    const seed = new (await loadOutbox()).BridgeOutbox(root);
    await (seed as any).writeRejectionDiagnostic(oversized, serializedBytes, 'serialized-size-limit');
    const [finalName] = (await readdir(quarantine)).filter((name) => name.startsWith('rejected-'));
    assert.ok(finalName);
    const liveTemp = join(quarantine, `.${finalName}.live-writer.tmp`);
    await rename(join(quarantine, finalName), liveTemp);
    liveHandle = await openFile(liveTemp, 'r');

    const recovering = new (await loadOutbox()).BridgeOutbox(root);
    await (recovering as any).writeRejectionDiagnostic(oversized, serializedBytes, 'serialized-size-limit');

    assert.equal(await pathExists(liveTemp), true);
    assert.equal(await pathExists(join(quarantine, finalName)), true);
  } finally {
    await liveHandle?.close().catch(() => undefined);
    await rm(root, { recursive: true, force: true });
  }
});

test('matching concurrent rejection writers converge on one durable final', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-bridge-outbox-matching-rejection-'));
  try {
    const quarantine = join(root, 'quarantine');
    await mkdir(quarantine, { mode: 0o700 });
    const oversized = oversizedEvent('matching-event', 'matching-key');
    const serializedBytes = Buffer.byteLength(JSON.stringify(oversized), 'utf8');
    const first = new (await loadOutbox()).BridgeOutbox(root);
    const second = new (await loadOutbox()).BridgeOutbox(root);

    const outcomes = await Promise.allSettled([
      (first as any).writeRejectionDiagnostic(oversized, serializedBytes, 'serialized-size-limit'),
      (second as any).writeRejectionDiagnostic(oversized, serializedBytes, 'serialized-size-limit'),
    ]);

    assert.deepEqual(outcomes.map((outcome) => outcome.status), ['fulfilled', 'fulfilled']);
    assert.equal((await readdir(quarantine)).filter((name) => name.startsWith('rejected-')).length, 1);
    assert.equal((await readdir(quarantine)).filter((name) => name.endsWith('.tmp')).length, 0);
    const finalName = (await readdir(quarantine)).find((name) => name.startsWith('rejected-'));
    assert.ok(finalName);
    assert.equal(JSON.parse(await readFile(join(quarantine, finalName), 'utf8')).serializedBytes, serializedBytes);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('conflicting concurrent rejection writers never overwrite and fail closed', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-bridge-conflicting-rejection-'));
  try {
    const quarantine = join(root, 'quarantine');
    await mkdir(quarantine, { mode: 0o700 });
    const firstEvent = oversizedEvent('same-event-id', 'same-event-key');
    const secondEvent = event({
      eventId: 'same-event-id',
      eventKey: 'same-event-key',
      type: 'edit',
      observedAt: 1_700_000_000_001,
      payload: { messageId: 'same-event-id', text: 'different payload' },
    });
    const firstBytes = Buffer.byteLength(JSON.stringify(firstEvent), 'utf8');
    const secondBytes = firstBytes + 1;
    const first = new (await loadOutbox()).BridgeOutbox(root);
    const second = new (await loadOutbox()).BridgeOutbox(root);

    const outcomes = await Promise.allSettled([
      (first as any).writeRejectionDiagnostic(firstEvent, firstBytes, 'serialized-size-limit'),
      (second as any).writeRejectionDiagnostic(secondEvent, secondBytes, 'serialized-size-limit'),
    ]);

    assert.deepEqual(outcomes.map((outcome) => outcome.status).sort(), ['fulfilled', 'rejected']);
    const finalName = (await readdir(quarantine)).find((name) => name.startsWith('rejected-'));
    assert.ok(finalName);
    const finalDiagnostic = JSON.parse(await readFile(join(quarantine, finalName), 'utf8'));
    const winnerIndex = outcomes.findIndex((outcome) => outcome.status === 'fulfilled');
    const winner = winnerIndex === 0
      ? { event: firstEvent, serializedBytes: firstBytes }
      : { event: secondEvent, serializedBytes: secondBytes };
    assert.equal(finalDiagnostic.observedAt, winner.event.observedAt);
    assert.equal(finalDiagnostic.serializedBytes, winner.serializedBytes);
    assert.equal(finalDiagnostic.type, winner.event.type);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('matching rejection final is idempotent and conflicting final blocks source removal', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-bridge-outbox-rejection-conflict-'));
  try {
    const oversized = oversizedEvent('conflicting-final-event', 'conflicting-final-key');
    await writeFile(join(root, eventFileName(oversized)), JSON.stringify(oversized), { mode: 0o600 });
    const quarantine = join(root, 'quarantine');
    await mkdir(quarantine, { mode: 0o700 });
    const serializedBytes = Buffer.byteLength(JSON.stringify(oversized), 'utf8');
    const seed = new (await loadOutbox()).BridgeOutbox(root);
    await (seed as any).writeRejectionDiagnostic(oversized, serializedBytes, 'serialized-size-limit');
    await (seed as any).writeRejectionDiagnostic(oversized, serializedBytes, 'serialized-size-limit');
    const [finalName] = (await readdir(quarantine)).filter((name) => name.startsWith('rejected-'));
    assert.ok(finalName);
    assert.equal((await readdir(quarantine)).filter((name) => name.startsWith('rejected-')).length, 1);
    await writeFile(join(quarantine, finalName), '{"conflict":true}', { mode: 0o600 });

    const restarted = new (await loadOutbox()).BridgeOutbox(root);
    await assert.rejects(restarted.open(), /Conflicting bridge rejection diagnostic/);
    assert.equal(await pathExists(join(root, eventFileName(oversized))), true);
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
