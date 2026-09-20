import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
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

function inboundMessage(messageId: string): Record<string, unknown> {
  return {
    key: { remoteJid: '12345@s.whatsapp.net', id: messageId },
    message: { conversation: 'durable inbound message' },
    messageTimestamp: 1_700_000_000,
  };
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
