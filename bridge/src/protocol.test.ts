import test from 'node:test';
import assert from 'node:assert/strict';

import {
  createErrorResponse,
  createOkResponse,
  parseBridgeCommand,
  PROTOCOL_VERSION,
} from './protocol.js';

test('parseBridgeCommand accepts valid v2 command', () => {
  const parsed = parseBridgeCommand({
    version: PROTOCOL_VERSION,
    type: 'send_text',
    token: 'secret',
    requestId: 'req-1',
    payload: {
      to: '12345@s.whatsapp.net',
      text: 'hello',
    },
  });

  assert.equal(parsed.ok, true);
  if (parsed.ok) {
    assert.equal(parsed.command.type, 'send_text');
    assert.equal(parsed.command.requestId, 'req-1');
  }
});

test('parseBridgeCommand rejects legacy command shape', () => {
  const parsed = parseBridgeCommand({
    type: 'send',
    to: '12345@s.whatsapp.net',
    text: 'hello',
  });

  assert.equal(parsed.ok, false);
  if (!parsed.ok) {
    assert.equal(parsed.error.code, 'ERR_PROTOCOL_VERSION');
  }
});

test('parseBridgeCommand rejects invalid token', () => {
  const parsed = parseBridgeCommand({
    version: PROTOCOL_VERSION,
    type: 'health',
    token: '',
    payload: {},
  });

  assert.equal(parsed.ok, false);
  if (!parsed.ok) {
    assert.equal(parsed.error.code, 'ERR_AUTH');
  }
});

test('parseBridgeCommand rejects non-object payload', () => {
  const parsed = parseBridgeCommand({
    version: PROTOCOL_VERSION,
    type: 'health',
    token: 'secret',
    payload: 'bad-shape',
  });

  assert.equal(parsed.ok, false);
  if (!parsed.ok) {
    assert.equal(parsed.error.code, 'ERR_SCHEMA');
  }
});

test('parseBridgeCommand accepts valid presence_update command', () => {
  const parsed = parseBridgeCommand({
    version: PROTOCOL_VERSION,
    type: 'presence_update',
    token: 'secret',
    payload: {
      state: 'composing',
      chatJid: '12345@g.us',
    },
  });

  assert.equal(parsed.ok, true);
  if (parsed.ok) {
    assert.equal(parsed.command.type, 'presence_update');
  }
});

test('parseBridgeCommand rejects invalid presence_update payload', () => {
  const parsed = parseBridgeCommand({
    version: PROTOCOL_VERSION,
    type: 'presence_update',
    token: 'secret',
    payload: {
      state: 'composing',
    },
  });

  assert.equal(parsed.ok, false);
  if (!parsed.ok) {
    assert.equal(parsed.error.code, 'ERR_SCHEMA');
  }
});

test('response envelope uses protocol v2', () => {
  const ok = createOkResponse({ requestId: 'req', accountId: 'default', result: { a: 1 } });
  const err = createErrorResponse({
    requestId: 'req',
    accountId: 'default',
    error: { code: 'ERR_SCHEMA', message: 'bad', retryable: false },
  });

  assert.equal(ok.version, PROTOCOL_VERSION);
  assert.equal(ok.type, 'response');
  assert.equal(err.version, PROTOCOL_VERSION);
  assert.equal(err.type, 'response');
});
