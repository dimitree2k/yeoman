import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { ConnectBudget, classifyWhatsAppDisconnect } from './connect_budget.js';

const HOUR = 3_600_000;

function budget(clock: { t: number }, file = join(mkdtempSync(join(tmpdir(), 'budget-')), 'b.json')) {
  return { file, b: new ConnectBudget(file, { now: () => clock.t }) };
}

test('logout is needs_human, 515 needs_restart, unknown transient', () => {
  assert.equal(classifyWhatsAppDisconnect(401).cls, 'needs_human');
  assert.equal(classifyWhatsAppDisconnect(515).cls, 'needs_restart');
  assert.equal(classifyWhatsAppDisconnect(undefined).cls, 'transient');
  assert.equal(classifyWhatsAppDisconnect(999).cls, 'transient');
});

test('five attempts per hour, then wait for the oldest to age out', () => {
  const clock = { t: 10 * HOUR };
  const { b } = budget(clock);
  for (let i = 0; i < 5; i += 1) {
    assert.equal(b.decide().action, 'connect');
    b.recordAttempt(false);
    clock.t += 1_000;
  }
  const d = b.decide();
  assert.equal(d.action, 'wait');
  assert.ok(d.action === 'wait' && d.why === 'hourly_limit' && d.ms > 59 * 60_000);
});

test('fifteen per day, then one probe every 8 h and no fresh budget', () => {
  const clock = { t: 100 * HOUR };
  const { b } = budget(clock);
  for (let i = 0; i < 15; i += 1) {
    clock.t += HOUR; // stays under the hourly limit
    assert.equal(b.decide().action, 'connect');
    b.recordAttempt(false);
  }
  let d = b.decide();
  assert.ok(d.action === 'wait' && d.why === 'budget_exhausted' && d.ms === 8 * HOUR);
  clock.t += 8 * HOUR;
  d = b.decide();
  assert.deepEqual(d, { action: 'connect', probe: true });
  b.recordAttempt(true);
  d = b.decide();
  assert.ok(d.action === 'wait' && d.why === 'budget_exhausted' && d.ms === 8 * HOUR);
});

test('needs_human holds with zero attempts and survives a restart', () => {
  const clock = { t: 5 * HOUR };
  const { b, file } = budget(clock);
  b.holdNeedsHuman(401, 'logged_out');
  const reloaded = new ConnectBudget(file, { now: () => clock.t + 30 * 24 * HOUR });
  const d = reloaded.decide();
  assert.equal(d.action, 'hold');
  reloaded.reset();
  assert.equal(new ConnectBudget(file).decide().action, 'connect');
});

test('a restart does not refill the budget', () => {
  const clock = { t: 50 * HOUR };
  const { b, file } = budget(clock);
  for (let i = 0; i < 5; i += 1) b.recordAttempt(false);
  assert.equal(new ConnectBudget(file, { now: () => clock.t }).decide().action, 'wait');
});
