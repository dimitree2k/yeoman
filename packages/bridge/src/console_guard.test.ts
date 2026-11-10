import assert from 'node:assert/strict';
import test from 'node:test';

import { REDACTED_BINARY, installConsoleGuard, redactConsoleArgs } from './console_guard.js';

function sessionLike() {
  // The shape libsignal prints on "Closing session:" (synthetic bytes).
  return {
    registrationId: 1,
    currentRatchet: {
      ephemeralKeyPair: { pubKey: Buffer.from([5, 1, 2]), privKey: Buffer.from([9, 9, 9]) },
      rootKey: Buffer.from([7, 7]),
    },
    indexInfo: { baseKeyType: 1, closed: -1 },
  };
}

test('session records with key buffers are redacted, the message text stays', () => {
  const out = redactConsoleArgs(['Closing session:', sessionLike()]);
  assert.deepEqual(out, ['Closing session:', REDACTED_BINARY]);
});

test('plain values and errors pass through unchanged', () => {
  const err = new Error('boom');
  const plain = { state: 'open', attempts: 2 };
  assert.deepEqual(redactConsoleArgs(['x', 3, plain, err]), ['x', 3, plain, err]);
});

test('installed guard never lets a private key reach the sink', () => {
  const lines: string[] = [];
  const sink = {
    log: (...a: unknown[]) => lines.push(a.map(String).join(' ')),
    info: (...a: unknown[]) => lines.push(a.map((v) => JSON.stringify(v)).join(' ')),
    warn: () => {},
    error: () => {},
    debug: () => {},
    trace: () => {},
  } as unknown as Console;
  installConsoleGuard(sink);
  installConsoleGuard(sink); // idempotent
  sink.info('Removing old closed session:', sessionLike());
  assert.equal(lines.length, 1);
  assert.ok(!lines[0].includes('privKey'));
  assert.ok(lines[0].includes('Removing old closed session'));
});
