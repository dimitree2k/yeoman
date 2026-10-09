import test from 'node:test';
import assert from 'node:assert/strict';

import {
  createErrorResponse,
  createOkResponse,
  deriveProviderEventIdentity,
  parseBridgeCommand,
  PROTOCOL_VERSION,
} from './protocol.js';

test('protocol version gates deterministic message ids', () => {
  // v5 adds the authenticated event subscription and durable event ACKs.
  assert.equal(PROTOCOL_VERSION, 6);
});

test('test_membership_change_identity_uses_logical_change_id', () => {
  const identity = (sourceCopyId: string) => deriveProviderEventIdentity(
    'membership_change' as any, 'account-1',
    { chatJid: 'chat@g.us', changeId: 'change-1', sourceCopyId },
  );
  const stub = identity('stub:message-1');
  const update = identity('update:copy-1');
  assert.ok(stub);
  assert.ok(update);
  assert.notEqual(stub.eventId, update.eventId);
  assert.equal(stub.eventKey, update.eventKey);
  assert.deepEqual(identity('stub:message-1'), stub);
});

test('test_membership_snapshot_identity_is_account_chat_and_time_scoped', () => {
  const identity = (account: string, chatJid: string, snapshotAtMs: number) =>
    deriveProviderEventIdentity('membership_snapshot' as any, account, { chatJid, snapshotAtMs });
  const first = identity('account-1', 'chat@g.us', 1700000000000);
  assert.ok(first);
  assert.deepEqual(first, identity('account-1', 'chat@g.us', 1700000000000));
  assert.notEqual(first.eventId, identity('account-2', 'chat@g.us', 1700000000000)?.eventId);
  assert.notEqual(first.eventId, identity('account-1', 'other@g.us', 1700000000000)?.eventId);
  assert.notEqual(first.eventId, identity('account-1', 'chat@g.us', 1700000000001)?.eventId);
});

test('edit event identities distinguish same-second revisions and deduplicate exact replay', () => {
  const identity = (payload: Record<string, unknown>) =>
    deriveProviderEventIdentity('edit', 'account-1', payload)?.eventId;
  const green = {
    chatJid: 'chat@g.us',
    messageId: 'target-1',
    timestamp: 1_700_000_123,
    text: 'grün',
  };
  const red = { ...green, text: 'rot' };

  assert.notEqual(identity(green), identity(red));
  assert.equal(identity(green), identity({ ...green }));
});

test('edit event identities distinguish legacy revisions without timestamps', () => {
  const identity = (payload: Record<string, unknown>) =>
    deriveProviderEventIdentity('edit', 'account-1', payload)?.eventId;
  const green = { chatJid: 'chat@g.us', messageId: 'target-2', text: 'grün' };

  assert.notEqual(identity(green), identity({ ...green, text: 'rot' }));
  assert.equal(identity(green), identity({ ...green }));
});

test('provider edit revision remains the preferred identity', () => {
  const identity = (payload: Record<string, unknown>) =>
    deriveProviderEventIdentity('edit', 'account-1', payload)?.eventId;

  assert.equal(
    identity({ chatJid: 'chat@g.us', messageId: 'target-3', revision: 2, text: 'grün' }),
    identity({ chatJid: 'chat@g.us', messageId: 'target-3', revision: 2, text: 'rot' }),
  );
});

test('legacy edit identity is stable when media metadata property order differs', () => {
  const identity = (media: Record<string, unknown>) =>
    deriveProviderEventIdentity('edit', 'account-1', {
      chatJid: 'chat@g.us',
      messageId: 'target-media',
      text: '[Document]',
      media,
    })?.eventId;
  const media = {
    kind: 'document',
    mimeType: 'application/pdf',
    fileName: 'report.pdf',
    bytes: 42,
    sha256: 'ab'.repeat(32),
  };

  const reordered = Object.fromEntries(Object.entries(media).reverse());
  assert.equal(identity(media), identity(reordered));
  assert.notEqual(identity(media), identity({ ...media, sha256: 'cd'.repeat(32) }));
});

test('parseBridgeCommand accepts event subscription and exact ACK commands', () => {
  const subscribe = parseBridgeCommand({
    version: PROTOCOL_VERSION,
    type: 'subscribe_events',
    token: 'secret',
    requestId: 'req-subscribe',
    payload: {},
  });
  const ack = parseBridgeCommand({
    version: PROTOCOL_VERSION,
    type: 'ack_event',
    token: 'secret',
    requestId: 'req-ack',
    payload: { eventId: 'event-1' },
  });

  assert.equal(subscribe.ok, true);
  assert.equal(ack.ok, true);
});

test('parseBridgeCommand accepts a valid command', () => {
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

test('parseBridgeCommand accepts native forward_message with an exact source', () => {
  const parsed = parseBridgeCommand({
    version: PROTOCOL_VERSION,
    type: 'forward_message',
    token: 'secret',
    requestId: 'req-forward-1',
    payload: {
      to: 'target@g.us',
      sourceChatJid: 'source@g.us',
      sourceMessageId: 'SRC-1',
    },
  });

  assert.equal(parsed.ok, true);
  if (parsed.ok) {
    assert.equal(parsed.command.type, 'forward_message');
    assert.deepEqual(parsed.command.payload, {
      to: 'target@g.us',
      sourceChatJid: 'source@g.us',
      sourceMessageId: 'SRC-1',
    });
  }
});

test('parseBridgeCommand rejects forward_message with empty identities', () => {
  const parsed = parseBridgeCommand({
    version: PROTOCOL_VERSION,
    type: 'forward_message',
    token: 'secret',
    payload: {
      to: 'target@g.us',
      sourceChatJid: ' ',
      sourceMessageId: '',
    },
  });

  assert.equal(parsed.ok, false);
  if (!parsed.ok) assert.equal(parsed.error.code, 'ERR_SCHEMA');
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

for (const [name, type, result] of [
  ['poll_result_uses_normalized_question_options', 'send_poll', { sent: { to: 'target@g.us', options: 2,
    poll: { name: 'Lunch?', values: ['one', 'two'], selectableCount: 1 } } }],
  ['forward_result_preserves_sent_content', 'forward_message', { forwarded: { to: 'target@g.us', content: {
    text: 'sent', caption: null, media: null, forwarded: true, sourceChatJid: 'source@g.us',
    sourceMessageId: 'SOURCE', provenance: 'sent' } } }],
  ['delete_result_keeps_target_not_new_message_id', 'delete_message', { deleted: { chatJid: 'target@g.us', messageId: 'TARGET' } }],
] as const) {
  test(`${name}: protocol validates additive result fields`, async () => {
    const protocol = await import('./protocol.js');
    const validate = (protocol as any).validateOutboundResult;
    assert.equal(typeof validate, 'function');
    assert.equal(validate(type, result), true);
    assert.equal(validate(type, { invalid: {} }), false);
    assert.equal(validate(type, { ...result, [Object.keys(result)[0]]: { ...Object.values(result)[0],
      ...(type === 'send_poll' ? { poll: { name: 1, values: [], selectableCount: true } } :
         type === 'forward_message' ? { content: { ...(result as any).forwarded.content, media: { kind: 'image', mediaKey: 'secret' } } } :
         { messageId: 17 }) } }), false);
  });
}

test('outbound_result_validator_parity', async () => {
  const { validateOutboundResult } = await import('./protocol.js');
  const poll = { name: 'Lunch?', values: ['one', 'two'], selectableCount: 1 };
  const content = { text: 'sent', caption: null, media: null, forwarded: true,
    sourceChatJid: '4915550000000-1@g.us', sourceMessageId: 'SOURCE', provenance: 'sent' };
  const cases: [string, string, Record<string, unknown>, boolean][] = [
    ['supplementary-name', 'send_poll', { sent: { to: 'target@g.us', options: 2, poll: { ...poll, name: '😀'.repeat(300) } } }, false],
    ['blank-source', 'forward_message', { forwarded: { to: 'target@g.us', content: { ...content, sourceChatJid: ' ' } } }, false],
    ['unsafe-bytes', 'forward_message', { forwarded: { to: 'target@g.us', content: { ...content, media: { kind: 'image', bytes: 9007199254740992 } } } }, false],
    ['array-provenance', 'forward_message', { forwarded: { to: 'target@g.us', content: { ...content, provenance: ['sent'] } } }, false],
    ['utf16-limit', 'send_poll', { sent: { to: 'target@g.us', options: 2, poll: { ...poll, name: '😀'.repeat(256) } } }, true],
    ['utf16-over-limit', 'send_poll', { sent: { to: 'target@g.us', options: 2, poll: { ...poll, name: '😀'.repeat(257) } } }, false],
    ['blank-name', 'send_poll', { sent: { to: 'target@g.us', options: 2, poll: { ...poll, name: ' \ufeff ' } } }, false],
    ['blank-option', 'send_poll', { sent: { to: 'target@g.us', options: 2, poll: { ...poll, values: ['one', '\ufeff'] } } }, false],
    ['safe-bytes', 'forward_message', { forwarded: { to: 'target@g.us', content: { ...content, media: { kind: 'image', bytes: 9007199254740991 } } } }, true],
    ['padded-source', 'forward_message', { forwarded: { to: 'target@g.us', content: { ...content, sourceChatJid: ` ${content.sourceChatJid} ` } } }, true],
    ['blank-message', 'forward_message', { forwarded: { to: 'target@g.us', content: { ...content, sourceMessageId: '\ufeff' } } }, false],
    ['js-nonwhitespace', 'forward_message', { forwarded: { to: 'target@g.us', content: { ...content, sourceMessageId: '\u0085' } } }, true],
    ['integral-number', 'send_poll', { sent: { to: 'target@g.us', options: 2, poll: { ...poll, selectableCount: 1.0 } } }, true],
    ['fractional-number', 'send_poll', { sent: { to: 'target@g.us', options: 2, poll: { ...poll, selectableCount: 1.5 } } }, false],
    ['boolean-number', 'send_poll', { sent: { to: 'target@g.us', options: 2, poll: { ...poll, selectableCount: true } } }, false],
  ];
  const results = cases.map(([name, type, result, expected]) => [name, validateOutboundResult(type, result), expected]);
  assert.ok(results.every(([, actual, expected]) => actual === expected), JSON.stringify(results));
});

test('group_metadata_snapshot_not_historical_change validator mirror', async () => {
  const { validGroupMetadata } = await import('./protocol.js');
  const payload = { chatJid: 'members@g.us', value: '', snapshot: true, observedAtMs: 1700000000000 };
  assert.equal(validGroupMetadata(payload), true);
  for (const changes of [{ value: null }, { snapshot: 1 }, { actorJid: '123@lid' },
    { occurredMs: 1700000000000 }, { observedAtMs: true }, { observedAtMs: 9007199254740992 },
    { extra: 'x' }, { chatJid: '123@lid' }, { value: '😀'.repeat(131073) }]) {
    assert.equal(validGroupMetadata({ ...payload, ...changes }), false);
  }
  assert.equal(validGroupMetadata({ chatJid: payload.chatJid, snapshot: true, observedAtMs: payload.observedAtMs }), false);
  assert.equal(validGroupMetadata({ ...payload, snapshot: false, actorJid: 'members@g.us', occurredMs: 1700000000000 }), true);
});

test('history mentions require a boolean and resolved phone JIDs', () => {
  for (const type of ['send_text', 'send_media']) {
    const payload = type === 'send_text' ? { to: 'synthetic@g.us', text: 'hello' }
      : { to: 'synthetic@g.us', mediaPath: '/synthetic/image.png' };
    const parse = (extra: object) => parseBridgeCommand({ version: PROTOCOL_VERSION,
      token: 'synthetic-token', requestId: 'synthetic-id', type, payload: { ...payload, ...extra } });
    assert.equal(parse({ historyMentionsResolved: true, mentions: ['491000000001@s.whatsapp.net'] }).ok, true);
    for (const mentions of [['100000000001@lid'], ['100000000001'], ['alice@s.whatsapp.net']]) {
      assert.equal(parse({ historyMentionsResolved: true, mentions }).ok, false);
    }
    assert.equal(parse({ historyMentionsResolved: 'true' }).ok, false);
    assert.equal(parse({ historyMentionsResolved: true }).ok, true);
    assert.equal(parse({ mentions: ['100000000001@lid'] }).ok, true);
  }
});
