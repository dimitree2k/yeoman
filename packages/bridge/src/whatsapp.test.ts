import test from 'node:test';
import assert from 'node:assert/strict';
import { chmod, mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
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
import { createEventEnvelope } from './protocol.js';

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

async function waitFor(predicate: () => boolean, timeoutMs = 500): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!predicate() && Date.now() < deadline) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

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
