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
