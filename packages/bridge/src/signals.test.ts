import assert from 'node:assert/strict';
import test from 'node:test';

import { PROTOCOL_VERSION, parseBridgeCommand } from './protocol.js';

test('v4 accepts lookup_message and rejects unknown types', () => {
  const parsed = parseBridgeCommand({
    version: PROTOCOL_VERSION,
    type: 'lookup_message',
    token: 'secret',
    requestId: 'req-1',
    payload: { chatJid: 'chat@g.us', messageId: '3EB0' },
  });
  assert.equal(parsed.ok, true);

  const rejected = parseBridgeCommand({
    version: PROTOCOL_VERSION,
    type: 'not_a_command',
    token: 'secret',
    payload: {},
  });
  assert.equal(rejected.ok, false);
});

test('signal event types are part of the protocol', async () => {
  const protocol = await import('./protocol.js');
  assert.equal(protocol.PROTOCOL_VERSION, 4);
  // The four signal kinds are emitted as events, so the envelope accepts them.
  const envelope = protocol.createEventEnvelope({
    type: 'reaction',
    accountId: 'a',
    payload: { chatJid: 'chat@g.us', targetMessageId: '3EB0', emoji: 'x' },
  });
  assert.equal(envelope.type, 'reaction');
  assert.equal(envelope.version, 4);
});
