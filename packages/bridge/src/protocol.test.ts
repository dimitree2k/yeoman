import test from 'node:test';
import assert from 'node:assert/strict';

import {
  createErrorResponse,
  createOkResponse,
  parseBridgeCommand,
  PROTOCOL_VERSION,
} from './protocol.js';

test('protocol version gates deterministic message ids', () => {
  // v4 adds the edit/delete/reaction/receipt signals and lookup_message.
  assert.equal(PROTOCOL_VERSION, 4);
});

test('parseBridgeCommand accepts valid v3 command', () => {
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

test('parseBridgeCommand accepts delete_message with an exact target', () => {
  const parsed = parseBridgeCommand({
    version: PROTOCOL_VERSION,
    type: 'delete_message',
    token: 'secret',
    requestId: 'req-delete-1',
    payload: {
      chatJid: '12345@s.whatsapp.net',
      messageId: 'BAE5EXACTMESSAGEID',
    },
  });

  assert.equal(parsed.ok, true);
  if (parsed.ok) {
    assert.equal(parsed.command.type, 'delete_message');
    assert.deepEqual(parsed.command.payload, {
      chatJid: '12345@s.whatsapp.net',
      messageId: 'BAE5EXACTMESSAGEID',
    });
  }
});

test('parseBridgeCommand rejects delete_message without a message id', () => {
  const parsed = parseBridgeCommand({
    version: PROTOCOL_VERSION,
    type: 'delete_message',
    token: 'secret',
    payload: {
      chatJid: '12345@s.whatsapp.net',
    },
  });

  assert.equal(parsed.ok, false);
  if (!parsed.ok) {
    assert.equal(parsed.error.code, 'ERR_SCHEMA');
  }
});

test('parseBridgeCommand accepts send_text with replyToMessageId', () => {
  const parsed = parseBridgeCommand({
    version: PROTOCOL_VERSION,
    type: 'send_text',
    token: 'secret',
    requestId: 'req-2',
    payload: {
      to: '12345@s.whatsapp.net',
      text: 'hello',
      replyToMessageId: 'ABCDEF',
    },
  });

  assert.equal(parsed.ok, true);
  if (parsed.ok) {
    assert.equal(parsed.command.type, 'send_text');
  }
});

test('parseBridgeCommand preserves a deterministic clientMessageId', () => {
  const parsed = parseBridgeCommand({
    version: PROTOCOL_VERSION,
    type: 'send_text',
    token: 'secret',
    requestId: 'req-idempotent',
    payload: {
      to: '12345@s.whatsapp.net',
      text: 'hello',
      clientMessageId: 'A1B2C3D4E5F60708',
    },
  });

  assert.equal(parsed.ok, true);
  if (parsed.ok) {
    assert.equal(parsed.command.payload.clientMessageId, 'A1B2C3D4E5F60708');
  }
});

test('parseBridgeCommand rejects malformed clientMessageId', () => {
  const parsed = parseBridgeCommand({
    version: PROTOCOL_VERSION,
    type: 'send_text',
    token: 'secret',
    requestId: 'req-bad-idempotent',
    payload: {
      to: '12345@s.whatsapp.net',
      text: 'hello',
      clientMessageId: 'bad id',
    },
  });

  assert.equal(parsed.ok, false);
  if (!parsed.ok) {
    assert.equal(parsed.error.code, 'ERR_SCHEMA');
  }
});

test('parseBridgeCommand accepts send_text with mentions', () => {
  const parsed = parseBridgeCommand({
    version: PROTOCOL_VERSION,
    type: 'send_text',
    token: 'secret',
    requestId: 'req-mentions',
    payload: {
      to: '12345@g.us',
      text: '@12345 hello',
      mentions: ['12345@lid'],
    },
  });

  assert.equal(parsed.ok, true);
  if (parsed.ok) {
    assert.equal(parsed.command.type, 'send_text');
  }
});

test('parseBridgeCommand accepts send_media with mediaPath', () => {
  const parsed = parseBridgeCommand({
    version: PROTOCOL_VERSION,
    type: 'send_media',
    token: 'secret',
    requestId: 'req-3',
    payload: {
      to: '12345@s.whatsapp.net',
      mediaPath: '/tmp/sample.ogg',
      mimeType: 'audio/ogg',
      replyToMessageId: 'ABCDEF',
    },
  });

  assert.equal(parsed.ok, true);
  if (parsed.ok) {
    assert.equal(parsed.command.type, 'send_media');
  }
});

test('parseBridgeCommand rejects send_text with malformed mentions', () => {
  const parsed = parseBridgeCommand({
    version: PROTOCOL_VERSION,
    type: 'send_text',
    token: 'secret',
    requestId: 'req-bad-mentions',
    payload: {
      to: '12345@g.us',
      text: '@12345 hello',
      mentions: '12345@lid',
    },
  });

  assert.equal(parsed.ok, false);
  if (!parsed.ok) {
    assert.equal(parsed.error.code, 'ERR_SCHEMA');
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

test('response envelope uses protocol v3', () => {
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
