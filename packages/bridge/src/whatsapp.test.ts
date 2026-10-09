import test from 'node:test';
import assert from 'node:assert/strict';
import { chmod, mkdir, mkdtemp, readdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import {
  FALLBACK_WHATSAPP_WEB_VERSION,
  WhatsAppClient,
  mediaExtension,
  resolveParticipantJid,
  resolveWhatsAppWebVersion,
  shouldIgnoreFromMeInbound,
} from './whatsapp.js';
import { BridgeServer } from './server.js';
import { BridgeOutbox } from './outbox.js';
import { MAX_BRIDGE_FRAME_BYTES, createEventEnvelope, deriveEditSignalIdentity, deriveProviderEventIdentity } from './protocol.js';
import { proto } from '@whiskeysockets/baileys/WAProto/index.js';

function inboundMessage(messageId: string): Record<string, unknown> {
  return {
    key: { remoteJid: '12345@s.whatsapp.net', id: messageId },
    message: { conversation: 'durable inbound message' },
    messageTimestamp: 1_700_000_000,
  };
}

function testClient(
  onMessage: (message: any) => void | Promise<void> = () => {},
  onSignal: (kind: string, payload: Record<string, unknown>) => void | Promise<void> = () => {},
): WhatsAppClient {
  return new WhatsAppClient({
    authDir: '/tmp/yeoman-whatsapp-payload-test',
    readReceipts: false,
    onMessage,
    onSignal: onSignal as any,
    onQR: () => {},
    onStatus: () => {},
    onError: () => {},
  });
}

async function waitFor(predicate: () => boolean, timeoutMs = 5000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!predicate() && Date.now() < deadline) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

function membershipStub(id: string, stubType: number, participants: unknown = ['123@lid']) {
  return {
    key: { remoteJid: 'members@g.us', id, participant: '456@lid' },
    messageStubType: stubType,
    messageStubParameters: participants,
    messageTimestamp: 1_700_000_000,
  };
}

async function membershipClient(t: any) {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-membership-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const signals: Array<{ kind: string; payload: any }> = [];
  const messages: unknown[] = [];
  const client = new WhatsAppClient({
    authDir: root,
    messageReferenceDir: join(root, 'references'),
    readReceipts: false,
    acceptFromMe: false,
    onMessage: (message) => { messages.push(message); },
    onSignal: (kind, payload) => { signals.push({ kind, payload }); },
    onQR: () => {}, onStatus: () => {}, onError: () => {},
  });
  await (client as any).referenceStore.open();
  (client as any).acceptingProviderEvents = true;
  return { client: client as any, signals, messages };
}

async function reactionClient(t: any) {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-reaction-capture-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const signals: Array<{ kind: string; payload: any }> = [];
  const client = new WhatsAppClient({
    authDir: root,
    messageReferenceDir: join(root, 'references'),
    readReceipts: false,
    onMessage: () => {},
    onSignal: (kind, payload) => { signals.push({ kind, payload }); },
    onQR: () => {}, onStatus: () => {}, onError: () => {},
  });
  return { client: client as any, signals };
}

async function capturedReaction(t: any, event: any, self?: string) {
  const { client, signals } = await reactionClient(t);
  client.acceptingProviderEvents = true;
  if (self) client.updateSelfIds({ creds: { me: { id: self } } });
  client.handleReactionEvents([event]);
  await client.drainProviderEvents();
  return signals[0]?.payload;
}

test('test_group_self_reaction_uses_authenticated_self_jid', async (t) => {
  const payload = await capturedReaction(t, {
    key: { remoteJid: 'friends@g.us', id: 'TARGET-1' },
    reaction: { key: { remoteJid: 'friends@g.us', fromMe: true }, text: '👍' },
  }, '4915202777685:4@s.whatsapp.net');
  assert.deepEqual(payload, {
    chatJid: 'friends@g.us', targetMessageId: 'TARGET-1', senderId: '4915202777685@s.whatsapp.net',
    emoji: '👍', removed: false,
  });
});

test('test_group_other_reaction_keeps_participant_jid', async (t) => {
  const payload = await capturedReaction(t, {
    key: { remoteJid: 'friends@g.us', id: 'TARGET-2' },
    reaction: { key: { remoteJid: 'friends@g.us', participant: 'other@lid', fromMe: false }, text: '❤️' },
  });
  assert.deepEqual(payload, {
    chatJid: 'friends@g.us', targetMessageId: 'TARGET-2', senderId: 'other@lid', emoji: '❤️', removed: false,
  });
});

test('test_group_missing_participant_from_me_false_has_no_group_actor', async (t) => {
  const payload = await capturedReaction(t, {
    key: { remoteJid: 'friends@g.us', id: 'TARGET-3' },
    reaction: { key: { remoteJid: 'friends@g.us', fromMe: false }, text: '🔥' },
  }, '4915202777685@s.whatsapp.net');
  assert.equal(payload?.senderId, '');
  assert.notEqual(payload?.senderId, 'friends@g.us');
  assert.equal(payload?.chatJid, 'friends@g.us');
  assert.equal(payload?.targetMessageId, 'TARGET-3');
  assert.equal(payload?.emoji, '🔥');
  assert.equal(payload?.removed, false);
});

test('test_direct_reaction_keeps_direct_actor', async (t) => {
  const payload = await capturedReaction(t, {
    key: { remoteJid: '12345@s.whatsapp.net', id: 'TARGET-4' },
    reaction: { key: { remoteJid: '12345@s.whatsapp.net', fromMe: false }, text: '🙂' },
  });
  assert.deepEqual(payload, {
    chatJid: '12345@s.whatsapp.net', targetMessageId: 'TARGET-4', senderId: '12345@s.whatsapp.net',
    emoji: '🙂', removed: false,
  });
});

test('test_group_self_reaction_removal_keeps_actor', async (t) => {
  const payload = await capturedReaction(t, {
    key: { remoteJid: 'friends@g.us', id: 'TARGET-5' },
    reaction: { key: { remoteJid: 'friends@g.us', fromMe: true }, text: '' },
  }, '4915202777685@s.whatsapp.net');
  assert.deepEqual(payload, {
    chatJid: 'friends@g.us', targetMessageId: 'TARGET-5', senderId: '4915202777685@s.whatsapp.net',
    emoji: '', removed: true,
  });
});

test('test_group_membership_stub_emits_without_message_content', async (t) => {
  const { client, signals, messages } = await membershipClient(t);
  const stubs = [
    [proto.WebMessageInfo.StubType.GROUP_PARTICIPANT_ADD, 'add'],
    [proto.WebMessageInfo.StubType.GROUP_PARTICIPANT_INVITE, 'add'],
    [proto.WebMessageInfo.StubType.GROUP_PARTICIPANT_REMOVE, 'remove'],
    [proto.WebMessageInfo.StubType.GROUP_PARTICIPANT_LEAVE, 'remove'],
    [proto.WebMessageInfo.StubType.GROUP_PARTICIPANT_PROMOTE, 'promote'],
    [proto.WebMessageInfo.StubType.GROUP_PARTICIPANT_DEMOTE, 'demote'],
  ] as const;
  for (const [stub, action] of stubs) {
    await client.handleInboundMessage(membershipStub(`stub-${stub}`, stub, [JSON.stringify({ id: '123@lid' })]));
    const emitted = signals.at(-1);
    assert.equal(emitted?.kind, 'membership_change');
    assert.equal(emitted?.payload.stubType, stub);
    assert.equal(emitted?.payload.action, action);
    assert.equal(emitted?.payload.messageId, `stub-${stub}`);
    assert.equal(emitted?.payload.providerTimestampMs, 1_700_000_000_000);
    assert.deepEqual(emitted?.payload.participants, [{ lid: '123@lid' }]);
    assert.deepEqual(emitted?.payload.actor, { lid: '456@lid' });
    assert.match(emitted?.payload.sourceCopyId, /^stub:/);
    assert.equal('message' in emitted?.payload, false);
    assert.equal('text' in emitted?.payload, false);
  }
  assert.equal(signals.length, 6);
  assert.equal(messages.length, 0);
  await client.handleInboundMessage(membershipStub('unknown', 99999));
  assert.equal(signals.length, 6);
});

test('test_group_participants_update_emits_actor_and_participants', async (t) => {
  const { client, signals, messages } = await membershipClient(t);
  const handlers = new Map<string, (update: any) => unknown>();
  client.sock = { ev: { on: (name: string, handler: (update: any) => unknown) => handlers.set(name, handler) } };
  client.registerInboundMessageHandler();
  const handler = handlers.get('group-participants.update');
  assert.ok(handler);
  await handler({
    id: 'members@g.us', action: 'modify', author: '456@lid', authorPn: '491456@s.whatsapp.net',
    participants: [{ id: '123@lid', phoneNumber: '491123@s.whatsapp.net', admin: 'admin', unused: 'omit' }],
  });
  await client.drainProviderEvents();
  assert.equal(signals.length, 1);
  assert.equal(signals[0].kind, 'membership_change');
  assert.equal(signals[0].payload.action, 'modify');
  assert.equal(signals[0].payload.providerTimestampMs, null);
  assert.deepEqual(signals[0].payload.actor, { lid: '456@lid', phoneJid: '491456@s.whatsapp.net' });
  assert.deepEqual(signals[0].payload.participants, [{ lid: '123@lid', phoneJid: '491123@s.whatsapp.net' }]);
  assert.equal('admin' in signals[0].payload.participants[0], false);
  assert.equal(messages.length, 0);
  await handler({ id: 'members@g.us', action: 'unknown', participants: ['123@lid'] });
  await handler({ id: 'members@g.us', action: 'add', participants: 'malformed' });
  await client.drainProviderEvents();
  assert.equal(signals.length, 1);
});

test('membership stubs retain unknown native identifiers and timestamps', async (t) => {
  const { client, signals } = await membershipClient(t);
  await client.handleInboundMessage({
    key: { remoteJid: 'members@g.us' },
    messageStubType: proto.WebMessageInfo.StubType.GROUP_PARTICIPANT_ADD,
    messageStubParameters: ['123@lid'],
  });
  assert.equal(signals.length, 1);
  assert.equal(signals[0].payload.providerTimestampMs, null);
  assert.equal('messageId' in signals[0].payload, false);
  assert.equal('actor' in signals[0].payload, false);
  assert.equal(signals[0].payload.stubType, proto.WebMessageInfo.StubType.GROUP_PARTICIPANT_ADD);
  assert.ok(signals[0].payload.changeId);
  await client.handleInboundMessage(membershipStub('bad-json',
    proto.WebMessageInfo.StubType.GROUP_PARTICIPANT_ADD, ['{bad-json']));
  assert.equal(signals.length, 1);
});

test('membership changes include known phone mappings without inventing conflicting pairs', async (t) => {
  const { client, signals } = await membershipClient(t);
  client.lidToPhone.set('123', '491123@s.whatsapp.net');
  client.lidToPhone.set('456', '491456@s.whatsapp.net');
  await client.handleInboundMessage(membershipStub('mapped', proto.WebMessageInfo.StubType.GROUP_PARTICIPANT_ADD));
  assert.deepEqual(signals[0].payload.participants, [{ lid: '123@lid', phoneJid: '491123@s.whatsapp.net' }]);
  assert.deepEqual(signals[0].payload.actor, { lid: '456@lid', phoneJid: '491456@s.whatsapp.net' });
  client.lidConflicts.add('123');
  await client.handleInboundMessage(membershipStub('conflicted', proto.WebMessageInfo.StubType.GROUP_PARTICIPANT_ADD));
  assert.deepEqual(signals[1].payload.participants, [{ lid: '123@lid' }]);
});

test('test_stub_and_participant_update_share_change_key_in_both_orders', async (t) => {
  for (const stubFirst of [true, false]) {
    const { client, signals } = await membershipClient(t);
    const handlers = new Map<string, (update: any) => unknown>();
    client.sock = { ev: { on: (name: string, handler: (update: any) => unknown) => handlers.set(name, handler) } };
    client.registerInboundMessageHandler();
    const handler = handlers.get('group-participants.update');
    assert.ok(handler);
    const stub = async (id: string) => client.handleInboundMessage(
      membershipStub(id, proto.WebMessageInfo.StubType.GROUP_PARTICIPANT_ADD),
    );
    const update = async () => {
      await handler({ id: 'members@g.us', action: 'add', author: '456@lid',
        participants: [{ id: '123@lid', phoneNumber: '491123@s.whatsapp.net' }] });
      await client.drainProviderEvents();
    };
    // Two equal signatures must pair FIFO, rather than collapsing real changes.
    if (stubFirst) { await stub('first'); await stub('second'); await update(); await update(); }
    else { await update(); await update(); await stub('first'); await stub('second'); }
    assert.equal(signals.length, 4);
    assert.equal(signals[0].payload.changeId, signals[2].payload.changeId);
    assert.equal(signals[1].payload.changeId, signals[3].payload.changeId);
    assert.notEqual(signals[0].payload.changeId, signals[1].payload.changeId);
    assert.equal(new Set(signals.map(({ payload }) => payload.sourceCopyId)).size, 4);
    await stub('first');
    assert.equal(signals.length, 4);
  }
});

test('test_from_me_membership_stub_is_captured_and_correlated_with_from_me_disabled', async (t) => {
  const { client, signals, messages } = await membershipClient(t);
  const handlers = new Map<string, (update: any) => unknown>();
  client.sock = { ev: { on: (name: string, handler: (update: any) => unknown) => handlers.set(name, handler) } };
  client.registerInboundMessageHandler();
  const handler = handlers.get('group-participants.update');
  assert.ok(handler);
  const native = {
    ...membershipStub('self-member-1', proto.WebMessageInfo.StubType.GROUP_PARTICIPANT_ADD,
      [JSON.stringify({ id: '123@lid', phoneNumber: '491123@s.whatsapp.net' })]),
    key: { remoteJid: 'members@g.us', id: 'self-member-1', participant: '456@lid', fromMe: true },
    message: { conversation: 'self stub text must not invoke the ordinary callback' },
  };
  await client.handleInboundMessage(native);
  await handler({ id: 'members@g.us', action: 'add', author: '456@lid',
    participants: [{ id: '123@lid', phoneNumber: '491123@s.whatsapp.net' }] });
  await client.drainProviderEvents();
  assert.equal(signals.length, 2);
  const [stub, update] = signals;
  assert.equal(stub.kind, 'membership_change');
  assert.equal(update.kind, 'membership_change');
  assert.equal(stub.payload.changeId, update.payload.changeId);
  assert.notEqual(stub.payload.sourceCopyId, update.payload.sourceCopyId);
  const stubIdentity = deriveProviderEventIdentity('membership_change', 'default', stub.payload);
  const updateIdentity = deriveProviderEventIdentity('membership_change', 'default', update.payload);
  assert.ok(stubIdentity);
  assert.ok(updateIdentity);
  assert.equal(stubIdentity.eventKey, updateIdentity.eventKey);
  assert.notEqual(stubIdentity.eventId, updateIdentity.eventId);
  assert.equal(stub.payload.messageId, 'self-member-1');
  assert.equal(stub.payload.stubType, proto.WebMessageInfo.StubType.GROUP_PARTICIPANT_ADD);
  assert.equal(stub.payload.providerTimestampMs, 1_700_000_000_000);
  assert.deepEqual(stub.payload.actor, { lid: '456@lid' });
  assert.deepEqual(stub.payload.participants, [{ lid: '123@lid', phoneJid: '491123@s.whatsapp.net' }]);
  const retained = await client.referenceStore.get('members@g.us', 'self-member-1') as any;
  assert.ok(retained);
  assert.equal(retained.key.fromMe, true);
  assert.equal(retained.key.id, 'self-member-1');
  assert.equal(retained.messageStubType, native.messageStubType);
  assert.deepEqual(retained.messageStubParameters, native.messageStubParameters);
  assert.equal(Number(retained.messageTimestamp), 1_700_000_000);
  assert.equal(messages.length, 0);
  await client.handleInboundMessage({
    key: { remoteJid: 'members@g.us', id: 'self-text-1', participant: '456@lid', fromMe: true },
    message: { conversation: 'ordinary self text remains filtered' },
    messageTimestamp: 1_700_000_001,
  });
  assert.equal(signals.length, 2);
  assert.equal(messages.length, 0);
  assert.equal(await client.referenceStore.has('members@g.us', 'self-text-1'), false);
});

test('test_connect_emits_one_bounded_snapshot_per_group', async (t) => {
  const { client, signals } = await membershipClient(t);
  client.connected = true;
  let fetches = 0;
  client.sock = { groupFetchAllParticipating: async () => {
    fetches += 1;
    return {
      'members@g.us': { participants: [
        { id: '123@lid', phoneNumber: '491123@s.whatsapp.net', admin: 'admin', name: 'omit' },
      ] },
      'empty@g.us': { participants: [] },
      'unknown@g.us': {},
      'huge@g.us': { participants: Array.from({ length: 6000 }, (_, index) => ({
        id: `${index + 100000}@lid`, phoneNumber: `${index + 490000}@s.whatsapp.net`, admin: 'admin',
      })) },
    };
  } };
  await client.refreshLidCache();
  assert.equal(fetches, 1);
  assert.equal(signals.length, 4);
  for (const { kind, payload } of signals) {
    assert.equal(kind, 'membership_snapshot');
    assert.deepEqual(Object.keys(payload).sort(), ['chatJid', 'complete', 'memberCount', 'participants', 'snapshotAtMs']);
    assert.ok(Buffer.byteLength(JSON.stringify(createEventEnvelope({ type: kind as any, payload }))) <= MAX_BRIDGE_FRAME_BYTES);
  }
  assert.deepEqual(signals[0].payload.participants, [{ lid: '123@lid', phoneJid: '491123@s.whatsapp.net', admin: true }]);
  assert.equal(signals[0].payload.complete, true);
  assert.equal(signals[1].payload.complete, true);
  assert.deepEqual(signals[1].payload.participants, []);
  assert.equal(signals[2].payload.complete, false);
  assert.equal(signals[3].payload.complete, false);
  assert.equal(signals[3].payload.memberCount, 6000);
  assert.deepEqual(signals[3].payload.participants, []);
  const firstAt = signals[0].payload.snapshotAtMs;
  await client.refreshLidCache();
  assert.notEqual(signals[4].payload.snapshotAtMs, firstAt);
});

test('membership correlation expires after 30 seconds and retains at most 256 pending copies', async (t) => {
  const { client, signals } = await membershipClient(t);
  const handlers = new Map<string, (update: any) => unknown>();
  client.sock = { ev: { on: (name: string, handler: (update: any) => unknown) => handlers.set(name, handler) } };
  client.registerInboundMessageHandler();
  const handler = handlers.get('group-participants.update');
  assert.ok(handler);
  const originalNow = Date.now;
  let current = originalNow();
  t.mock.method(Date, 'now', () => current);
  await client.handleInboundMessage(membershipStub('expired', proto.WebMessageInfo.StubType.GROUP_PARTICIPANT_ADD));
  current += 30_001;
  await handler({ id: 'members@g.us', action: 'add', participants: [{ id: '123@lid' }] });
  await client.drainProviderEvents();
  assert.notEqual(signals[0].payload.changeId, signals[1].payload.changeId);
  for (let index = 0; index < 257; index++) {
    await client.handleInboundMessage(membershipStub(`bounded-${index}`,
      proto.WebMessageInfo.StubType.GROUP_PARTICIPANT_ADD, [`${index + 1000}@lid`]));
  }
  await handler({ id: 'members@g.us', action: 'add', participants: [{ id: '1000@lid' }] });
  await client.drainProviderEvents();
  assert.notEqual(signals[2].payload.changeId, signals.at(-1)?.payload.changeId);
  await handler({ id: 'members@g.us', action: 'add', participants: [{ id: '1256@lid' }] });
  await client.drainProviderEvents();
  assert.equal(signals[258].payload.changeId, signals.at(-1)?.payload.changeId);
});

test('resolveParticipantJid ignores quoted participant metadata in direct chat', () => {
  const msg = {
    key: { participant: '86728660521036@lid' },
    participant: '86728660521036@lid',
    message: {
      extendedTextMessage: {
        contextInfo: { participant: '86728660521036@lid' },
      },
    },
  };

  const resolved = resolveParticipantJid(msg, '34596062240904@lid', false);
  assert.equal(resolved, '34596062240904@lid');
});

test('resolveParticipantJid keeps group participant when available', () => {
  const msg = {
    key: { participant: '272661821259976@lid' },
  };

  const resolved = resolveParticipantJid(msg, '491786127564-1611913127@g.us', true);
  assert.equal(resolved, '272661821259976@lid');
});

test('resolveParticipantJid falls back to remote JID in groups when participant missing', () => {
  const msg = {};

  const resolved = resolveParticipantJid(msg, '491786127564-1611913127@g.us', true);
  assert.equal(resolved, '491786127564-1611913127@g.us');
});

test('a client without an explicit reference dir honours BRIDGE_MESSAGE_REFERENCE_DIR', async (t) => {
  const previous = process.env.BRIDGE_MESSAGE_REFERENCE_DIR;
  const override = await mkdtemp(join(tmpdir(), 'yeoman-default-refdir-'));
  t.after(async () => {
    if (previous === undefined) delete process.env.BRIDGE_MESSAGE_REFERENCE_DIR;
    else process.env.BRIDGE_MESSAGE_REFERENCE_DIR = previous;
    await rm(override, { recursive: true, force: true });
  });
  process.env.BRIDGE_MESSAGE_REFERENCE_DIR = override;

  // Without the override this resolves to the live runtime store; an earlier run left
  // six synthetic records there because exactly this client took the default path.
  const client = new WhatsAppClient({
    authDir: join(override, 'auth'),
    onMessage: () => {},
    onQR: () => {},
    onStatus: () => {},
    onError: () => {},
  });
  assert.equal((client as any).referenceStore.directory, override);
  await (client as any).referenceStore.open();
  assert.equal(
    await (client as any).referenceStore.put('123@g.us', 'ENV-1', inboundMessage('ENV-1')),
    true,
  );
  assert.ok((await readdir(override)).length > 0);
});

test('refreshLidCache keeps conflicting mappings blocked instead of overwriting', async () => {
  const statuses: Array<{ name: string; payload: unknown }> = [];
  const client = new WhatsAppClient({
    authDir: '/tmp/yeoman-lid-conflict-test',
    onMessage: () => {},
    onQR: () => {},
    onStatus: (name, payload) => statuses.push({ name, payload }),
    onError: () => {},
  });
  (client as any).connected = true;
  let phone = '491700000001@s.whatsapp.net';
  (client as any).sock = {
    groupFetchAllParticipating: async () => ({
      'group@g.us': { participants: [{ id: '123@lid', phoneNumber: phone }] },
    }),
  };

  await (client as any).refreshLidCache();
  assert.equal(client.resolvePhoneJid('123@lid'), phone);
  phone = '491700000002@s.whatsapp.net';
  await (client as any).refreshLidCache();

  assert.equal(client.resolvePhoneJid('123@lid'), undefined);
  assert.equal((client as any).isLidConflict('123@lid'), true);
  assert.equal(statuses.some(({ name }) => name === 'lid_mapping_conflict'), true);
});

test('shouldIgnoreFromMeInbound drops self messages by default', () => {
  assert.equal(shouldIgnoreFromMeInbound(true, false, false), true);
  assert.equal(shouldIgnoreFromMeInbound(true, undefined, false), true);
});

test('shouldIgnoreFromMeInbound accepts user self messages when flag enabled', () => {
  assert.equal(shouldIgnoreFromMeInbound(true, true, false), false);
  assert.equal(shouldIgnoreFromMeInbound(false, false, false), false);
});

test('shouldIgnoreFromMeInbound ignores bridge-sent self messages when flag enabled', () => {
  assert.equal(shouldIgnoreFromMeInbound(true, true, true), true);
});

test('mediaExtension preserves document file names and maps PDF mime type', () => {
  assert.equal(mediaExtension('document', 'application/pdf', undefined), '.pdf');
  assert.equal(mediaExtension('document', undefined, 'Frank Report.PDF'), '.pdf');
});

test('inbound text is not truncated at 8000 characters', async () => {
  const text = 'x'.repeat(8_001);
  let received: any;
  const client = testClient((message) => {
    received = message;
  });
  (client as any).sock = { readMessages: async () => undefined };

  await (client as any).processInboundMessage(
    {
      key: { remoteJid: '12345@s.whatsapp.net', id: 'long-message' },
      message: { conversation: text },
      messageTimestamp: 1_700_000_000,
    },
    '12345@s.whatsapp.net',
    '12345@s.whatsapp.net',
    'long-message',
  );

  assert.equal(received.text, text);
});

test('inbound provider text preserves leading and trailing whitespace', async () => {
  const text = "  exact provider text \n\t";
  let received: any;
  const client = testClient((message) => {
    received = message;
  });
  (client as any).sock = { readMessages: async () => undefined };

  await (client as any).processInboundMessage(
    {
      key: { remoteJid: '12345@s.whatsapp.net', id: 'whitespace-message' },
      message: { conversation: text },
      messageTimestamp: 1_700_000_000,
    },
    '12345@s.whatsapp.net',
    '12345@s.whatsapp.net',
    'whitespace-message',
  );

  assert.equal(received.text, text);
});

test('media captions preserve provider text without synthetic labels', () => {
  const client = testClient();
  const caption = '  exact caption \n\t';
  const messages = [
    { imageMessage: { caption, mimetype: 'image/jpeg' } },
    { videoMessage: { caption, mimetype: 'video/mp4' } },
    { documentMessage: { caption, mimetype: 'text/plain', fileName: 'note.txt' } },
  ];

  for (const message of messages) {
    const extracted = (client as any).extractMessageTextAndMedia({ message });
    assert.equal(extracted.text, caption);
  }
});

test('media-only inbound messages carry metadata without binary payloads', () => {
  const client = testClient();
  const providerHash = Buffer.alloc(32, 0xab);
  const extracted = (client as any).extractMessageTextAndMedia({
    message: {
      documentMessage: {
        mimetype: 'application/pdf',
        fileName: 'report.pdf',
        fileLength: '42',
        fileSha256: providerHash,
      },
    },
  });

  assert.equal(extracted.text, '[Document]');
  assert.deepEqual(extracted.media, {
    kind: 'document',
    mimeType: 'application/pdf',
    fileName: 'report.pdf',
    bytes: 42,
    sha256: providerHash.toString('hex'),
  });
  assert.equal('data' in extracted.media, false);
  assert.equal('base64' in extracted.media, false);
});

test('PDF extraction is not attempted and the envelope only contains caption metadata', () => {
  const client = testClient();
  const extracted = (client as any).extractMessageTextAndMedia({
    message: {
      documentMessage: {
        mimetype: 'application/pdf',
        fileName: 'report.pdf',
        caption: 'Please review page one',
        fileLength: 128,
        fileSha256: Uint8Array.from(Buffer.alloc(32, 0xcd)),
      },
    },
  });

  assert.equal(extracted.text, 'Please review page one');
  assert.deepEqual(extracted.media, {
    kind: 'document',
    mimeType: 'application/pdf',
    fileName: 'report.pdf',
    bytes: 128,
    sha256: 'cd'.repeat(32),
  });
  assert.equal(Object.keys(extracted.media).some((key) => /data|buffer|base64|text/i.test(key)), false);
});

test('media hash selection ignores invalid higher-priority values', () => {
  const client = testClient();
  const extracted = (client as any).extractMessageTextAndMedia({
    message: {
      documentMessage: {
        mimetype: 'application/pdf',
        fileName: 'report.pdf',
        sha256: 'not-a-sha256',
        hash: '',
        fileSha256: Uint8Array.from(Buffer.alloc(32, 0xef)),
      },
    },
  });

  assert.equal(extracted.media.sha256, 'ef'.repeat(32));

  const uppercase = (client as any).extractMessageTextAndMedia({
    message: {
      documentMessage: {
        mimetype: 'application/pdf',
        fileName: 'uppercase.pdf',
        sha256: 'AB'.repeat(32),
      },
    },
  });
  assert.equal(uppercase.media.sha256, 'ab'.repeat(32));
});

test('edit signals preserve replacement text and provider revision when present', () => {
  const client = testClient();
  const payload = (client as any).extractEditPayload({
    key: {
      remoteJid: 'chat@g.us',
      id: 'provider-edit-id',
      participant: '4915@s.whatsapp.net',
    },
    update: {
      messageTimestamp: 1_700_000_123,
      revision: 3,
      message: {
        editedMessage: {
          message: { conversation: 'replacement text', revision: 3 },
        },
      },
    },
  });

  assert.deepEqual(payload, {
    chatJid: 'chat@g.us',
    messageId: 'provider-edit-id',
    participantJid: '4915@s.whatsapp.net',
    timestamp: 1_700_000_123,
    text: 'replacement text',
    revision: 3,
  });
});

test('local edit signal identity is chat-scoped and shares deterministic revision fallback', () => {
  const green = {
    chatJid: 'chat@g.us',
    messageId: 'target-4',
    timestamp: 1_700_000_123,
    text: 'grün',
  };
  const red = { ...green, text: 'rot' };
  assert.notEqual(deriveEditSignalIdentity(green), deriveEditSignalIdentity(red));
  assert.equal(deriveEditSignalIdentity(green), deriveEditSignalIdentity({ ...green }));
  assert.notEqual(
    deriveEditSignalIdentity(green),
    deriveEditSignalIdentity({ ...green, chatJid: 'other-chat@g.us' }),
  );

  const withoutTimestamp = { chatJid: 'chat@g.us', messageId: 'target-5', text: 'grün' };
  assert.notEqual(
    deriveEditSignalIdentity(withoutTimestamp),
    deriveEditSignalIdentity({ ...withoutTimestamp, text: 'rot' }),
  );
});

test('send results retain provider and client message ids separately', async () => {
  const client = testClient();
  (client as any).sock = {
    sendMessage: async () => ({ key: { id: 'provider-message-id' } }),
  };
  (client as any).connected = true;

  const result = await client.sendText(
    '12345@s.whatsapp.net',
    'hello',
    undefined,
    undefined,
    'client-message-id',
  );

  assert.deepEqual(result, {
    to: '12345@s.whatsapp.net',
    messageId: 'provider-message-id',
    providerMessageId: 'provider-message-id',
    clientMessageId: 'client-message-id',
  });
});

test('native forward passes the stored WAMessage to Baileys', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-forward-'));
  try {
    const source = proto.WebMessageInfo.fromObject({
      key: { remoteJid: 'source@g.us', id: 'SRC-1' },
      message: { imageMessage: { mimetype: 'image/jpeg', caption: 'image' } },
    });
    const client = new WhatsAppClient({
      authDir: join(root, 'auth'),
      messageReferenceDir: root,
      onMessage: () => {},
      onQR: () => {},
      onStatus: () => {},
      onError: () => {},
    });
    await (client as any).referenceStore.open();
    assert.equal(await (client as any).referenceStore.put('source@g.us', 'SRC-1', source), true);

    const calls: unknown[][] = [];
    (client as any).sock = {
      sendMessage: async (...args: unknown[]) => {
        calls.push(args);
        return { key: { id: 'OUT-1' } };
      },
    };
    (client as any).connected = true;

    await client.forwardMessage({
      to: 'target@g.us',
      sourceChatJid: 'source@g.us',
      sourceMessageId: 'SRC-1',
    });

    assert.deepEqual(calls, [['target@g.us', { forward: source }, {}]]);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('lookupMessage survives a store restart and reports a missing exact source', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-forward-'));
  try {
    const options = {
      authDir: join(root, 'auth'),
      messageReferenceDir: root,
      onMessage: () => {},
      onQR: () => {},
      onStatus: () => {},
      onError: () => {},
    };
    const source = proto.WebMessageInfo.fromObject({
      key: { remoteJid: 'source@g.us', id: 'SRC-2' },
      message: { conversation: 'stored' },
    });
    const first = new WhatsAppClient(options);
    await (first as any).referenceStore.open();
    assert.equal(await (first as any).referenceStore.put('source@g.us', 'SRC-2', source), true);

    const restarted = new WhatsAppClient(options);
    assert.deepEqual(await restarted.lookupMessage({ chatJid: 'source@g.us', messageId: 'SRC-2' }), {
      status: 'found',
      messageId: 'SRC-2',
    });
    assert.deepEqual(await restarted.lookupMessage({ chatJid: 'source@g.us', messageId: 'MISSING' }), {
      status: 'absent',
    });
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('group reaction uses the exact retained source key', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-reaction-'));
  try {
    const chatJid = 'group@g.us';
    const messageId = 'REACTION-SOURCE-1';
    const client = new WhatsAppClient({
      authDir: join(root, 'auth'),
      messageReferenceDir: root,
      onMessage: () => {},
      onQR: () => {},
      onStatus: () => {},
      onError: () => {},
    });
    const source = proto.WebMessageInfo.fromObject({
      key: {
        remoteJid: chatJid,
        id: messageId,
        fromMe: false,
        participant: 'sender@lid',
      },
      message: { conversation: 'source message' },
    });
    await (client as any).referenceStore.open();
    assert.equal(await (client as any).referenceStore.put(chatJid, messageId, source), true);
    const calls: unknown[][] = [];
    (client as any).sock = {
      sendMessage: async (...args: unknown[]) => {
        calls.push(args);
        return { key: { id: 'REACTION-OUT-1' } };
      },
    };
    (client as any).connected = true;

    await client.react({ chatJid, messageId, emoji: '👍' });

    assert.equal(calls.length, 1);
    assert.equal(calls[0]?.[0], chatJid);
    const payload = calls[0]?.[1] as { react: { key: Record<string, unknown>; text: string } };
    assert.equal(payload.react.text, '👍');
    assert.deepEqual({ ...payload.react.key }, {
      remoteJid: chatJid,
      id: messageId,
      fromMe: false,
      participant: 'sender@lid',
    });
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('group reaction fails closed when its exact retained source is missing', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-reaction-'));
  try {
    const source = proto.WebMessageInfo.fromObject({
      key: {
        remoteJid: 'other-group@g.us',
        id: 'SHARED-ID',
        fromMe: false,
        participant: 'other-sender@lid',
      },
      message: { conversation: 'message from another chat' },
    });
    const client = new WhatsAppClient({
      authDir: join(root, 'auth'),
      messageReferenceDir: root,
      onMessage: () => {},
      onQR: () => {},
      onStatus: () => {},
      onError: () => {},
    });
    await (client as any).referenceStore.open();
    assert.equal(await (client as any).referenceStore.put('other-group@g.us', 'SHARED-ID', source), true);
    const calls: unknown[][] = [];
    (client as any).sock = {
      sendMessage: async (...args: unknown[]) => {
        calls.push(args);
        return { key: { id: 'SHOULD-NOT-SEND' } };
      },
    };
    (client as any).connected = true;

    await assert.rejects(
      client.react({ chatJid: 'requested-group@g.us', messageId: 'SHARED-ID', emoji: '👍' }),
    );
    assert.deepEqual(calls, []);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('direct reaction still works without a retained source', async () => {
  const client = testClient();
  const calls: unknown[][] = [];
  (client as any).sock = {
    sendMessage: async (...args: unknown[]) => {
      calls.push(args);
      return { key: { id: 'DIRECT-REACTION-OUT' } };
    },
  };
  (client as any).connected = true;

  await client.react({
    chatJid: '12345@s.whatsapp.net',
    messageId: 'DIRECT-SOURCE',
    emoji: '👍',
    fromMe: true,
  });

  assert.deepEqual(calls, [[
    '12345@s.whatsapp.net',
    {
      react: {
        text: '👍',
        key: {
          remoteJid: '12345@s.whatsapp.net',
          id: 'DIRECT-SOURCE',
          fromMe: true,
          participant: undefined,
        },
      },
    },
    undefined,
  ]]);
});

test('native forward fails closed without sending when the exact source is missing', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-forward-'));
  try {
    const client = new WhatsAppClient({
      authDir: join(root, 'auth'),
      messageReferenceDir: root,
      onMessage: () => {},
      onQR: () => {},
      onStatus: () => {},
      onError: () => {},
    });
    const calls: unknown[] = [];
    (client as any).sock = {
      sendMessage: async (...args: unknown[]) => {
        calls.push(args);
        return { key: { id: 'never-sent' } };
      },
    };
    (client as any).connected = true;

    await assert.rejects(
      client.forwardMessage({
        to: 'target@g.us',
        sourceChatJid: 'source@g.us',
        sourceMessageId: 'MISSING',
      }),
      (error: any) => error.code === 'ERR_FORWARD_UNAVAILABLE' && error.retryable === false,
    );
    assert.deepEqual(calls, []);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('quoted-message backfill stores a complete exact provider envelope', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-forward-'));
  try {
    const client = new WhatsAppClient({
      authDir: join(root, 'auth'),
      messageReferenceDir: root,
      onMessage: () => {},
      onQR: () => {},
      onStatus: () => {},
      onError: () => {},
    });
    await (client as any).referenceStore.open();
    await (client as any).buildReplyMeta({
      message: {
        extendedTextMessage: {
          text: 'reply',
          contextInfo: {
            stanzaId: 'SRC-3',
            participant: 'sender@s.whatsapp.net',
            quotedMessage: { conversation: 'quoted source' },
          },
        },
      },
    }, 'source@g.us');

    const restored = await (client as any).referenceStore.get('source@g.us', 'SRC-3');
    assert.equal(restored.key.remoteJid, 'source@g.us');
    assert.equal(restored.key.participant, 'sender@s.whatsapp.net');
    assert.equal(restored.message.conversation, 'quoted source');
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('sendText resolves a durable quote after the in-memory quote window', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-forward-'));
  try {
    const source = proto.WebMessageInfo.fromObject({
      key: { remoteJid: 'source@g.us', id: 'SRC-4' },
      message: { conversation: 'yesterday' },
    });
    const client = new WhatsAppClient({
      authDir: join(root, 'auth'),
      messageReferenceDir: root,
      onMessage: () => {},
      onQR: () => {},
      onStatus: () => {},
      onError: () => {},
    });
    await (client as any).referenceStore.open();
    assert.equal(await (client as any).referenceStore.put('source@g.us', 'SRC-4', source), true);
    const calls: unknown[][] = [];
    (client as any).sock = {
      sendMessage: async (...args: unknown[]) => {
        calls.push(args);
        return { key: { id: 'OUT-4' } };
      },
    };
    (client as any).connected = true;

    await client.sendText('source@g.us', 'answer @12345', 'SRC-4', ['12345@s.whatsapp.net']);

    assert.equal((calls[0][2] as any).quoted.message.conversation, 'yesterday');
    assert.deepEqual(calls[0][1], {
      text: 'answer @12345',
      mentions: ['12345@s.whatsapp.net'],
    });
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('resolveWhatsAppWebVersion uses fetched latest version', async () => {
  const version = await resolveWhatsAppWebVersion(async () => ({
    version: [2, 3000, 1035194821],
    isLatest: true,
  }));

  assert.deepEqual(version, [2, 3000, 1035194821]);
});

test('resolveWhatsAppWebVersion falls back when fetch fails', async () => {
  const version = await resolveWhatsAppWebVersion(async () => {
    throw new Error('network unavailable');
  });

  assert.deepEqual(version, FALLBACK_WHATSAPP_WEB_VERSION);
});

test('fatal persistence failure halts provider intake before another event enters dedupe', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-bridge-fatal-intake-'));
  try {
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
      'default',
      root,
    );
    const client = new WhatsAppClient({
      authDir: root,
      messageReferenceDir: root,
      readReceipts: false,
      onMessage: (message) =>
        (server as any).trackProviderEvent((server as any).broadcastMessage(message)),
      onQR: () => {},
      onStatus: () => {},
      onError: () => {},
    });
    (server as any).wa = client;
    (server as any).outbox.append = async () => {
      throw new Error('durable append failed');
    };
    (client as any).sock = { readMessages: async () => undefined };
    (client as any).running = true;
    (client as any).connected = true;
    (client as any).acceptingProviderEvents = true;

    await (client as any).admitProviderEvent(() =>
      (client as any).handleInboundMessage(inboundMessage('fatal-event-1')),
    );

    assert.equal((server as any).persistenceFailure, true);
    assert.equal((client as any).acceptingProviderEvents, false);
    assert.equal((client as any).running, false);
    assert.equal((client as any).connected, false);
    assert.equal((client as any).recentInbound.size, 0);

    await (client as any).admitProviderEvent(() =>
      (client as any).handleInboundMessage(inboundMessage('must-not-enter-dedupe')),
    );
    assert.equal((client as any).recentInbound.size, 0);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('message handler forwards same provider identity conflicts to the outbox', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-bridge-handler-conflict-'));
  try {
    const errors: string[] = [];
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
      'default',
      root,
    );
    const client = new WhatsAppClient({
      authDir: root,
      messageReferenceDir: root,
      readReceipts: false,
      onMessage: (message) =>
        (server as any).trackProviderEvent((server as any).broadcastMessage(message)),
      onQR: () => {},
      onStatus: () => {},
      onError: (error) => errors.push(error),
    });
    (server as any).wa = client;
    (client as any).sock = { readMessages: async () => undefined };
    (client as any).acceptingProviderEvents = true;

    const first = {
      ...inboundMessage('handler-conflict-1'),
      message: { conversation: 'first provider text' },
    };
    const conflicting = {
      ...first,
      message: { conversation: 'second provider text' },
    };
    await (client as any).handleInboundMessage(first);
    await (client as any).handleInboundMessage(conflicting);

    assert.equal((client as any).droppedInboundDuplicates, 0);
    assert.equal(errors.some((error) => error.includes('Conflicting bridge outbox event')), true);
    assert.equal((await (server as any).outbox.pending()).length, 1);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('stop waits for an admitted provider handler before returning', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-bridge-provider-drain-'));
  try {
    let release!: () => void;
    let markCallbackStarted!: () => void;
    const callbackStarted = new Promise<void>((resolve) => {
      markCallbackStarted = resolve;
    });
    const blocked = new Promise<void>((resolve) => {
      release = resolve;
    });
    const client = new WhatsAppClient({
      authDir: root,
      messageReferenceDir: root,
      readReceipts: false,
      onMessage: async () => {
        markCallbackStarted();
        await blocked;
      },
      onQR: () => {},
      onStatus: () => {},
      onError: () => {},
    });
    (client as any).sock = { readMessages: async () => undefined };
    (client as any).running = true;
    (client as any).acceptingProviderEvents = true;

    const handler = (client as any).admitProviderEvent(() =>
      (client as any).handleInboundMessage(inboundMessage('drain-event-1')),
    );
    await callbackStarted;

    let stopped = false;
    const stopping = client.stop().then(() => {
      stopped = true;
    });
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(stopped, false);
    release();
    await handler;
    await stopping;
    assert.equal(stopped, true);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('concurrent duplicate messages retry after the leading handler fails', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-bridge-dedupe-message-'));
  try {
    let release!: () => void;
    let markFirstStarted!: () => void;
    const firstStarted = new Promise<void>((resolve) => {
      markFirstStarted = resolve;
    });
    const blocked = new Promise<void>((resolve) => {
      release = resolve;
    });
    let callbacks = 0;
    const client = new WhatsAppClient({
      authDir: root,
      messageReferenceDir: root,
      readReceipts: false,
      onMessage: async () => {
        callbacks += 1;
        if (callbacks === 1) {
          markFirstStarted();
          await blocked;
          throw new Error('first durable callback failed');
        }
      },
      onQR: () => {},
      onStatus: () => {},
      onError: () => {},
    });
    const listeners = new Map<string, (value: unknown) => void>();
    (client as any).sock = {
      readMessages: async () => undefined,
      ev: { on: (name: string, listener: (value: unknown) => void) => listeners.set(name, listener) },
    };
    (client as any).registerInboundMessageHandler();
    (client as any).running = true;
    (client as any).acceptingProviderEvents = true;
    const message = inboundMessage('same-message-key');
    const upsert = listeners.get('messages.upsert')!;

    upsert({ messages: [message], type: 'notify' });
    await firstStarted;
    upsert({ messages: [message], type: 'notify' });
    release();
    await waitFor(() => callbacks === 2);

    assert.equal(callbacks, 2);
    assert.equal((client as any).droppedInboundDuplicates, 0);
    assert.equal((client as any).recentInbound.size, 1);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('pre-callback message failure clears dedupe state for redelivery', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-bridge-dedupe-pre-callback-'));
  try {
    let callbacks = 0;
    const client = new WhatsAppClient({
      authDir: root,
      messageReferenceDir: root,
      readReceipts: false,
      onMessage: async () => {
        callbacks += 1;
      },
      onQR: () => {},
      onStatus: () => {},
      onError: () => {},
    });
    const listeners = new Map<string, (value: unknown) => void>();
    (client as any).sock = {
      readMessages: async () => undefined,
      ev: { on: (name: string, listener: (value: unknown) => void) => listeners.set(name, listener) },
    };
    (client as any).registerInboundMessageHandler();
    (client as any).running = true;
    (client as any).acceptingProviderEvents = true;
    const originalBuildReplyMeta = (client as any).buildReplyMeta;
    (client as any).buildReplyMeta = async () => {
      throw new Error('reply metadata failed');
    };
    const message = inboundMessage('pre-callback-key');
    const upsert = listeners.get('messages.upsert')!;

    upsert({ messages: [message], type: 'notify' });
    await waitFor(() => (client as any).providerDedupe.size === 0);
    assert.equal((client as any).recentInbound.size, 0);

    (client as any).buildReplyMeta = originalBuildReplyMeta;
    upsert({ messages: [message], type: 'notify' });
    await waitFor(() => callbacks === 1);
    assert.equal(callbacks, 1);
    assert.equal((client as any).recentInbound.size, 1);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('concurrent duplicate signals retry after the leading handler fails', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-bridge-dedupe-signal-'));
  try {
    let release!: () => void;
    let markFirstStarted!: () => void;
    const firstStarted = new Promise<void>((resolve) => {
      markFirstStarted = resolve;
    });
    const blocked = new Promise<void>((resolve) => {
      release = resolve;
    });
    let callbacks = 0;
    const client = new WhatsAppClient({
      authDir: root,
      messageReferenceDir: root,
      onMessage: () => {},
      onSignal: async () => {
        callbacks += 1;
        if (callbacks === 1) {
          markFirstStarted();
          await blocked;
          throw new Error('first signal callback failed');
        }
      },
      onQR: () => {},
      onStatus: () => {},
      onError: () => {},
    });
    (client as any).running = true;
    (client as any).acceptingProviderEvents = true;

    (client as any).emitSignal('edit', 'same-signal-key', { messageId: 'signal-1' });
    await firstStarted;
    (client as any).emitSignal('edit', 'same-signal-key', { messageId: 'signal-1' });
    release();
    await waitFor(() => callbacks === 2);

    assert.equal(callbacks, 2);
    assert.equal((client as any).recentInbound.size, 1);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('registered duplicate message survives fatal pre-rename failure for restart replay', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-bridge-dedupe-recovery-'));
  const eventId = 'recovery-event-1';
  const eventKey = 'recovery-key-1';
  const finalPath = join(
    root,
    `00000000000000000001-${encodeURIComponent(eventId)}-${encodeURIComponent(eventKey)}.json`,
  );
  try {
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
      'default',
      root,
    );
    await (server as any).outbox.open();
    // Force only the canonical rename to fail after the fsynced temp record exists.
    await mkdir(finalPath);
    await chmod(finalPath, 0o500);

    let callbacks = 0;
    const client = new WhatsAppClient({
      authDir: root,
      messageReferenceDir: root,
      readReceipts: false,
      onMessage: (message) => {
        callbacks += 1;
        return (server as any).trackProviderEvent(
          (server as any).broadcastReplayable(
            createEventEnvelope({
              type: 'message',
              eventId,
              eventKey,
              payload: { messageId: message.messageId, text: message.text },
            }),
          ),
        );
      },
      onQR: () => {},
      onStatus: () => {},
      onError: () => {},
    });
    (server as any).wa = client;
    const listeners = new Map<string, (value: any) => void>();
    (client as any).sock = {
      readMessages: async () => undefined,
      ev: { on: (name: string, listener: (value: any) => void) => listeners.set(name, listener) },
    };
    (client as any).registerInboundMessageHandler();
    (client as any).running = true;
    (client as any).connected = true;
    (client as any).acceptingProviderEvents = true;

    const message = inboundMessage('same-key-recovery');
    listeners.get('messages.upsert')!({ messages: [message, message], type: 'notify' });
    await waitFor(() => (server as any).persistenceFailure === true);
    assert.equal(callbacks, 1);
    assert.equal((client as any).acceptingProviderEvents, false);
    assert.equal((client as any).droppedInboundDuplicates, 0);

    await chmod(finalPath, 0o700);
    await rm(finalPath, { recursive: true, force: true });
    const restarted = new BridgeOutbox(root);
    await restarted.open();
    const pending = await restarted.pending();
    assert.equal(pending.length, 1);
    assert.equal(pending[0].eventId, eventId);
    assert.equal(pending[0].eventKey, eventKey);
    assert.equal((pending[0].payload as any).messageId, 'same-key-recovery');
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('deleteMessage sends a fromMe delete key for the exact target', async () => {
  const sent: Array<{ jid: string; payload: unknown }> = [];
  const client = new WhatsAppClient({
    authDir: '/tmp/yeoman-delete-message-test',
    onMessage: () => {},
    onQR: () => {},
    onStatus: () => {},
    onError: () => {},
  });

  (client as any).sock = {
    sendMessage: async (jid: string, payload: unknown) => {
      sent.push({ jid, payload });
      return { key: { id: 'delete-ack' } };
    },
  };
  (client as any).connected = true;

  const result = await client.deleteMessage({
    chatJid: '12345@s.whatsapp.net',
    messageId: 'BAE5EXACTMESSAGEID',
  });

  assert.deepEqual(result, {
    chatJid: '12345@s.whatsapp.net',
    messageId: 'BAE5EXACTMESSAGEID',
  });
  assert.deepEqual(sent, [
    {
      jid: '12345@s.whatsapp.net',
      payload: {
        delete: {
          remoteJid: '12345@s.whatsapp.net',
          fromMe: true,
          id: 'BAE5EXACTMESSAGEID',
        },
      },
    },
  ]);
});

test('sendMedia sends WAV and MP3 inputs through the voice PTT branch', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-voice-'));
  const sent: Array<{ jid: string; payload: any }> = [];
  const client = new WhatsAppClient({
    authDir: join(root, 'auth'),
    mediaOutgoingDir: root,
    onMessage: () => {},
    onQR: () => {},
    onStatus: () => {},
    onError: () => {},
  });
  (client as any).sock = {
    sendMessage: async (jid: string, payload: unknown) => {
      sent.push({ jid, payload });
      return { key: { id: 'voice-ack' } };
    },
  };
  (client as any).connected = true;

  try {
    for (const [name, mimeType] of [['voice.wav', 'audio/wav'], ['voice.mp3', 'audio/mpeg']]) {
      const path = join(root, name);
      await writeFile(path, Buffer.from('audio'));
      await client.sendMedia({ to: '12345@s.whatsapp.net', mediaPath: path, mimeType });
    }
  } finally {
    await rm(root, { recursive: true, force: true });
  }

  assert.deepEqual(
    sent.map(({ payload }) => ({
      audio: Buffer.isBuffer(payload.audio),
      ptt: payload.ptt,
      mimetype: payload.mimetype,
      document: payload.document,
    })),
    [
      { audio: true, ptt: true, mimetype: 'audio/wav', document: undefined },
      { audio: true, ptt: true, mimetype: 'audio/mpeg', document: undefined },
    ],
  );
});


test('getMessage reuses cached and persisted originals only within the requested key scope', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-edit-get-message-'));
  try {
    const referenceDir = join(root, 'references');
    const client = new WhatsAppClient({
      authDir: root,
      messageReferenceDir: referenceDir,
      onMessage: () => {},
      onQR: () => {},
      onStatus: () => {},
      onError: () => {},
    });
    const secret = Uint8Array.from({ length: 32 }, (_, index) => index + 1);
    const original = {
      key: {
        remoteJid: '123-456@g.us',
        id: 'original-1',
        fromMe: false,
        participant: '123@lid',
      },
      message: {
        conversation: 'original body',
        messageContextInfo: { messageSecret: secret },
      },
    };
    const store = (client as any).referenceStore;
    (client as any).storeInboundForQuote('123-456@g.us', 'original-1', original);
    const persistedGet = store.get.bind(store);
    store.get = async () => { throw new Error('disk lookup should not be needed'); };
    const request = {
      remoteJid: '123-456@g.us',
      id: 'original-1',
      fromMe: false,
      participant: '49123@s.whatsapp.net',
    };
    (client as any).lidToPhone.set('123', '49123@s.whatsapp.net');
    const cached = await (client as any).getMessage(request);
    assert.equal(cached.conversation, 'original body');
    assert.deepEqual(Buffer.from(cached.messageContextInfo.messageSecret), Buffer.from(secret));
    store.get = persistedGet;
    assert.equal(await store.put('123-456@g.us', 'original-1', original), true);
    const cacheEntry = (client as any).quoteCache.get('123-456@g.us:original-1');
    cacheEntry.msg = { ...original, message: { conversation: 'stale cache entry' } };
    cacheEntry.expiresAt = 0;
    assert.equal((await (client as any).getMessage(request)).conversation, 'original body');

    const restarted = new WhatsAppClient({
      authDir: root,
      messageReferenceDir: referenceDir,
      onMessage: () => {},
      onQR: () => {},
      onStatus: () => {},
      onError: () => {},
    });
    (restarted as any).lidToPhone.set('123', '49123@s.whatsapp.net');
    const restored = await (restarted as any).getMessage(request);
    assert.equal(restored.conversation, 'original body');
    assert.deepEqual(Buffer.from(restored.messageContextInfo.messageSecret), Buffer.from(secret));
    assert.equal(await (restarted as any).getMessage({ ...request, remoteJid: 'other@g.us' }), undefined);
    assert.equal(await (restarted as any).getMessage({ ...request, fromMe: true }), undefined);
    assert.equal(await (restarted as any).getMessage({ ...request, participant: '999@lid' }), undefined);
    (restarted as any).lidConflicts.add('123');
    assert.equal(await (restarted as any).getMessage(request), undefined);
    assert.equal(await (restarted as any).getMessage({ ...request, fromMe: undefined }), undefined);
    assert.equal(await (restarted as any).getMessage({ remoteJid: '123-456@g.us', id: 'missing' }), undefined);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('repeated encrypted edits are observation-only and leave the original secret available', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-encrypted-edit-observation-'));
  try {
    const observations: any[] = [];
    let readReceipts = 0;
    const client = new WhatsAppClient({
      authDir: root,
      messageReferenceDir: join(root, 'references'),
      readReceipts: true,
      onMessage: (message) => { observations.push(message); },
      onQR: () => {},
      onStatus: () => {},
      onError: () => {},
    });
    (client as any).acceptingProviderEvents = true;
    (client as any).sock = { readMessages: async () => { readReceipts += 1; } };
    const secret = Uint8Array.from({ length: 32 }, (_, index) => 255 - index);
    const original = {
      key: {
        remoteJid: '123-456@g.us',
        id: 'target-1',
        fromMe: false,
        participant: '123@lid',
      },
      message: {
        conversation: 'original text',
        messageContextInfo: { messageSecret: secret },
      },
      messageTimestamp: 1_700_000_000,
    };
    await (client as any).handleInboundMessage(original);
    readReceipts = 0;

    const encryptedEnvelope = (id: string) => ({
      key: {
        remoteJid: '123-456@g.us',
        id,
        fromMe: false,
        participant: '123@lid',
      },
      message: {
        secretEncryptedMessage: {
          encPayload: Uint8Array.from([1, 2, 3]),
          encIv: Uint8Array.from([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]),
          secretEncType: 2,
          targetMessageKey: {
            remoteJid: '123-456@g.us',
            id: 'target-1',
            fromMe: true,
            participant: '49123@s.whatsapp.net',
          },
        },
      },
      messageTimestamp: 1_700_000_001,
    });
    await (client as any).handleInboundMessage(encryptedEnvelope('edit-envelope-1'));
    await (client as any).handleInboundMessage(encryptedEnvelope('edit-envelope-2'));

    assert.equal(observations.length, 3);
    const [first, second] = observations.slice(1);
    for (const item of [first, second]) {
      assert.equal(item.text, '');
      assert.equal(item.observationOnly, true);
      assert.equal(item.observationType, 'encrypted_message_edit_undecoded');
      assert.equal(item.messageId.startsWith('edit-envelope-'), true);
      assert.equal(item.targetMessageId, 'target-1');
      assert.equal(item.encryptedEdit.encPayload, 'AQID');
      assert.equal(item.encryptedEdit.encIv, 'AAECAwQFBgcICQoL');
      assert.equal('messageSecret' in item.encryptedEdit, false);
    }
    assert.equal(readReceipts, 0);
    const stored = await (client as any).referenceStore.get('123-456@g.us', 'target-1');
    assert.equal(stored.message.conversation, 'original text');
    assert.deepEqual(Buffer.from(stored.message.messageContextInfo.messageSecret), Buffer.from(secret));
    assert.equal((await (client as any).getMessage({
      remoteJid: '123-456@g.us', id: 'target-1', fromMe: false, participant: '123@lid',
    })).conversation, 'original text');
    assert.equal(JSON.stringify([first, second]).includes('messageSecret'), false);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// Task 6: no provider connection; every client uses its own reference store.
async function outboundContractClient(t: any) {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-task6-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const client = new WhatsAppClient({ authDir: join(root, 'auth'), messageReferenceDir: join(root, 'references'),
    onMessage: () => {}, onQR: () => {}, onStatus: () => {}, onError: () => {} });
  (client as any).connected = true;
  return client;
}

test('poll_result_uses_normalized_question_options', async (t) => {
  const client = await outboundContractClient(t);
  const calls: any[] = [];
  (client as any).sock = { sendMessage: async (...args: any[]) => {
    calls.push(args); return { key: { id: 'POLL' } };
  } };
  const options = [' ', ...Array.from({ length: 14 }, (_, i) => ` option-${i} `)];
  const result = await client.sendPoll({ to: 'target@g.us', question: 'q'.repeat(600), options,
    maxSelections: 20, clientMessageId: 'CLIENT' });
  assert.deepEqual(JSON.parse(JSON.stringify(result)), { to: 'target@g.us', messageId: 'POLL',
    providerMessageId: 'POLL', clientMessageId: 'CLIENT', options: 14,
    poll: { name: 'q'.repeat(512), values: options.slice(1, 13).map(x => x.trim()), selectableCount: 12 } });
  assert.deepEqual((result as any).poll, calls[0][1].poll);
  assert.deepEqual(calls[0][2], { messageId: 'CLIENT' });
  const before = calls.length;
  await assert.rejects(client.sendPoll({ to: 'target@g.us', question: 'bad', options: [' ', 'one'] }));
  assert.equal(calls.length, before);
  (client as any).sock.sendMessage = async () => { throw new Error('provider failed'); };
  await assert.rejects(client.sendPoll({ to: 'target@g.us', question: 'valid', options: ['a', 'b'] }), /provider failed/);
});

test('forward_result_preserves_sent_content', async (t) => {
  const client = await outboundContractClient(t);
  await (client as any).referenceStore.open();
  const source = proto.WebMessageInfo.fromObject({ key: { remoteJid: 'source@g.us', id: 'SOURCE' },
    message: { imageMessage: { caption: 'source caption', mimetype: 'image/jpeg', fileLength: 12,
      mediaKey: Buffer.from('secret'), url: 'https://invalid.example/encrypted', directPath: '/encoded' } } });
  await (client as any).referenceStore.put('source@g.us', 'SOURCE', source);
  const input = { to: 'target@g.us', sourceChatJid: 'source@g.us', sourceMessageId: 'SOURCE', clientMessageId: 'CLIENT' };
  const calls: any[] = [];
  let body: any = { conversation: 'actually sent' };
  (client as any).sock = { sendMessage: async (...args: any[]) => {
    calls.push(args); return { key: { id: 'FORWARD' }, ...(body ? { message: body } : {}) };
  } };
  const sent = await client.forwardMessage(input);
  assert.deepEqual(JSON.parse(JSON.stringify(sent)), { to: input.to, messageId: 'FORWARD',
    providerMessageId: 'FORWARD', clientMessageId: 'CLIENT', content: { text: 'actually sent', caption: null,
      media: null, forwarded: true, sourceChatJid: input.sourceChatJid, sourceMessageId: 'SOURCE', provenance: 'sent' } });
  assert.deepEqual(calls[0], [input.to, { forward: source }, { messageId: 'CLIENT' }]);
  body = undefined;
  const fallback = await client.forwardMessage(input);
  assert.deepEqual((fallback as any).content, { text: null, caption: 'source caption',
    media: { kind: 'image', mimeType: 'image/jpeg', bytes: 12 }, forwarded: true,
    sourceChatJid: 'source@g.us', sourceMessageId: 'SOURCE', provenance: 'source' });
  const serialized = JSON.stringify(fallback);
  for (const forbidden of ['mediaKey', 'secret', 'url', 'directPath', 'encoded']) assert.ok(!serialized.includes(forbidden));
  const before = calls.length;
  await assert.rejects(client.forwardMessage({ ...input, sourceMessageId: 'MISSING' }));
  assert.equal(calls.length, before);
  (client as any).sock.sendMessage = async () => { throw new Error('provider failed'); };
  await assert.rejects(client.forwardMessage(input), /provider failed/);
});

test('delete_result_keeps_target_not_new_message_id', async (t) => {
  const client = await outboundContractClient(t);
  const calls: any[] = [];
  (client as any).sock = { sendMessage: async (...args: any[]) => {
    calls.push(args); return { key: { id: 'DELETE-ACK' } };
  } };
  const result = await client.deleteMessage({ chatJid: 'target@g.us', messageId: ' TARGET ' });
  assert.deepEqual(result, { chatJid: 'target@g.us', messageId: 'TARGET' });
  assert.deepEqual(calls, [['target@g.us', { delete: { remoteJid: 'target@g.us', fromMe: true, id: 'TARGET' } }]]);
  (client as any).sock.sendMessage = async () => { throw new Error('provider failed'); };
  await assert.rejects(client.deleteMessage({ chatJid: 'target@g.us', messageId: 'TARGET' }), /provider failed/);
});

test('group_metadata_snapshot_not_historical_change', async (t) => {
  const { client, signals } = await membershipClient(t);
  client.connected = true;
  client.sock = { groupFetchAllParticipating: async () => ({
    'members@g.us': { subject: ' exact subject ', desc: '', subjectOwner: '123@lid', subjectTime: 1 },
    'empty@g.us': {},
  }) };
  await client.refreshLidCache();
  const metadata = signals.filter((s: any) => s.kind.startsWith('group_'));
  assert.equal(metadata.length, 2);
  for (const { payload } of metadata) {
    assert.equal(payload.snapshot, true);
    assert.ok(Number.isSafeInteger(payload.observedAtMs));
    assert.equal(payload.actorJid, undefined);
    assert.equal(payload.occurredMs, undefined);
  }
  assert.equal(metadata[0].payload.value, ' exact subject ');
  assert.equal(metadata[1].payload.value, '');
  await client.handleGroupUpdate({ id: 'members@g.us', subject: ' exact subject ',
    subjectOwner: '123@lid', subjectTime: 1700000000, desc: '' });
  const updates = signals.filter((s: any) => s.kind.startsWith('group_') && !s.payload.snapshot);
  assert.equal(updates.length, 2);
  assert.equal(updates[0].payload.actorJid, '123@lid');
  assert.equal(updates[0].payload.occurredMs, 1700000000000);
  assert.equal(updates[1].payload.value, '');
  await client.handleGroupUpdate({ id: 'members@g.us' });
  assert.equal(signals.filter((s: any) => s.kind.startsWith('group_')).length, 4);
});

test('group_metadata_provider_fetch_snapshot_and_concurrent_author', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-group-provider-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const outbox = new BridgeOutbox(join(root, 'outbox'));
  await outbox.open();
  const handlers = new Map<string, (value: any) => unknown>();
  const captured: any[] = [];
  const client: any = new WhatsAppClient({
    authDir: root, messageReferenceDir: join(root, 'references'), readReceipts: false,
    onMessage: () => assert.fail('metadata opened a message'),
    onSignal: async (type, payload) => {
      if (!type.startsWith('group_')) return;
      const identity = deriveProviderEventIdentity(type, 'default', payload);
      captured.push(createEventEnvelope({ type, payload, ...identity }));
    },
    onQR: () => {}, onStatus: () => {}, onError: (error) => assert.fail(error),
  });
  await client.referenceStore.open();
  client.acceptingProviderEvents = true;
  client.connected = true;
  const fetched = { id: 'members@g.us', subject: 'current', desc: '', participants: [],
    subjectOwner: '111@lid', subjectTime: 1, descOwner: '111@lid', descTime: 2 };
  const second = { ...fetched, id: 'second@g.us', subject: 'second current' };
  client.sock = {
    ev: { on: (name: string, listener: (value: any) => unknown) => handlers.set(name, listener) },
    groupFetchAllParticipating: async () => {
      handlers.get('groups.update')!([fetched, second]); // installed Baileys fetch side effect
      handlers.get('groups.update')!([{ id: fetched.id, subject: 'concurrent',
        author: '222@lid', authorPn: '49222@s.whatsapp.net', subjectOwner: '111@lid' }]);
      return { [fetched.id]: fetched, [second.id]: second };
    },
  };
  client.registerInboundMessageHandler();
  await client.refreshLidCache();
  await waitFor(() => captured.length >= 5);
  await new Promise(resolve => setImmediate(resolve));
  const pending = captured;
  assert.equal(pending.length, 5);
  const snapshots = pending.filter(e => e.payload.snapshot);
  assert.equal(snapshots.length, 4);
  for (const { payload } of snapshots) {
    assert.ok((payload.observedAtMs as number) > 2000);
    assert.equal(payload.actorJid, undefined);
    assert.equal(payload.occurredMs, undefined);
  }
  const [change] = pending.filter(e => !e.payload.snapshot);
  assert.equal(change.payload.value, 'concurrent');
  assert.equal(change.payload.actorJid, '222@lid');
  assert.equal(change.payload.occurredMs, undefined);
  handlers.get('groups.update')!([{ id: fetched.id, desc: 'changed',
    author: '333@lid', authorPn: '49333@s.whatsapp.net' }]);
  await waitFor(() => captured.length >= 6);
  assert.equal(captured.filter(e => !e.payload.snapshot).length, 2);
  const description = captured.find(e => e.payload.value === 'changed')!;
  assert.equal(description.payload.actorJid, '333@lid');
  assert.equal(description.payload.occurredMs, undefined);
  // Provider dirty-group refresh outside reconnect also emits full metadata.
  handlers.get('groups.update')!([{ ...fetched, subject: 'later current' }]);
  await waitFor(() => captured.some(e => e.payload.value === 'later current'));
  assert.equal(captured.find(e => e.payload.value === 'later current').payload.snapshot, true);
  for (const envelope of captured) await outbox.append(envelope);
  assert.equal((await outbox.pending()).length, 8);
});

test('group_metadata_provider_author_listener', async (t) => {
  const { client, signals } = await membershipClient(t);
  const handlers = new Map<string, (value: any) => unknown>();
  client.sock = { ev: { on: (name: string, handler: (value: any) => unknown) => handlers.set(name, handler) } };
  client.registerInboundMessageHandler();
  handlers.get('groups.update')!([{ id: 'members@g.us', desc: 'native change',
    author: '222@lid', authorPn: '49222@s.whatsapp.net', descOwner: '111@lid' }]);
  await waitFor(() => signals.length === 1);
  assert.equal(signals[0].payload.actorJid, '222@lid');
  assert.equal(signals[0].payload.occurredMs, undefined);
  assert.equal(signals[0].payload.snapshot, false);
});


test('group_metadata_provider_buffered_fetch_single_snapshot', async (t) => {
  const { makeEventBuffer } = await import('@whiskeysockets/baileys/lib/Utils/event-buffer.js');
  const { default: pino } = await import('pino');
  const ev = makeEventBuffer(pino({ level: 'silent' }));
  t.after(() => ev.destroy());
  const { client, signals } = await membershipClient(t);
  client.connected = true;
  const metadata = { id: 'members@g.us', subject: 'current', desc: '', participants: [] };
  client.sock = { ev, groupFetchAllParticipating: async () => {
    ev.emit('groups.update', [metadata]);
    return { [metadata.id]: metadata };
  } };
  client.registerInboundMessageHandler();
  ev.buffer();
  await client.refreshLidCache();
  const before = signals.filter((s: any) => s.kind.startsWith('group_'));
  assert.equal(before.length, 2);
  ev.flush();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(signals.filter((s: any) => s.kind.startsWith('group_')).length, 2);
  assert.ok(before.every((s: any) => s.payload.snapshot && !s.payload.actorJid && !s.payload.occurredMs));
});

test('history mentions ignore stale Bridge mappings and refuse unresolved LIDs', async () => {
  const root = await mkdtemp(join(tmpdir(), 'history-mentions-'));
  try {
    const client = new WhatsAppClient({ authDir: join(root, 'auth'), messageReferenceDir: join(root, 'refs'),
      readReceipts: false, onMessage: () => {}, onQR: () => {}, onStatus: () => {}, onError: () => {} });
    const sent: any[] = [];
    (client as any).connected = true;
    (client as any).sock = { sendMessage: async (_to: string, payload: any) => { sent.push(payload); return { key: { id: 'synthetic' } }; } };
    (client as any).lidToPhone.set('491000000001', '491999999999@s.whatsapp.net');
    await client.sendText('synthetic@g.us', 'hello @491000000001', undefined, ['491000000001@s.whatsapp.net'], undefined, true);
    assert.deepEqual(sent[0].mentions, ['491000000001@s.whatsapp.net']);
    assert.equal(sent[0].text, 'hello @491000000001');
    await assert.rejects(client.sendText('synthetic@g.us', 'hello', undefined, ['100000000001@lid'], undefined, true));
    assert.equal(sent.length, 1);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
