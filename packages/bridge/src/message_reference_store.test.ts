import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readdir, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { proto } from '@whiskeysockets/baileys/WAProto/index.js';

import { MessageReferenceStore, defaultMessageReferenceDir } from './message_reference_store.js';

function message(conversation = 'hello') {
  return proto.WebMessageInfo.fromObject({
    key: { remoteJid: '123@g.us', id: 'ABC' },
    message: { conversation },
  });
}

test('message reference persists and reloads an exact provider envelope', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-message-ref-'));
  const storedAt = Date.now();
  const first = new MessageReferenceStore(root, { retentionMs: 7 * 24 * 60 * 60 * 1000 });
  await first.open();
  assert.equal(await first.put('123@g.us', 'ABC', message(), storedAt), true);

  const second = new MessageReferenceStore(root, { retentionMs: 7 * 24 * 60 * 60 * 1000 });
  await second.open();
  const restored = await second.get('123@g.us', 'ABC', storedAt + 1_000);
  assert.equal((restored as any).message.conversation, 'hello');
  assert.equal(await second.has('123@g.us', 'OTHER', storedAt + 1_000), false);
});

test('message reference expires at the retention boundary', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-message-ref-'));
  const store = new MessageReferenceStore(root, { retentionMs: 7 * 24 * 60 * 60 * 1000 });
  await store.open();
  assert.equal(await store.put('123@g.us', 'ABC', message(), 1_000), true);

  assert.equal(await store.has('123@g.us', 'ABC', 1_000 + 7 * 24 * 60 * 60 * 1000), false);
  assert.deepEqual(await readdir(root), []);
});

test('message reference open removes expired files', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-message-ref-'));
  const first = new MessageReferenceStore(root, { retentionMs: 1_000 });
  await first.open();
  assert.equal(await first.put('123@g.us', 'ABC', message(), Date.now() - 10_000), true);
  assert.equal((await readdir(root)).length, 1);

  const second = new MessageReferenceStore(root, { retentionMs: 1_000 });
  await second.open();
  assert.deepEqual(await readdir(root), []);
});

test('message reference rejects records above the configured bound', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-message-ref-'));
  const store = new MessageReferenceStore(root, { maxRecordBytes: 1 });
  await store.open();

  assert.equal(await store.put('123@g.us', 'ABC', message(), 1_000), false);
  assert.deepEqual(await readdir(root), []);
});

test('message reference directory and records are owner-only', async () => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-message-ref-'));
  const store = new MessageReferenceStore(root);
  await store.open();
  assert.equal(await store.put('123@g.us', 'ABC', message(), 1_000), true);

  assert.equal((await stat(root)).mode & 0o777, 0o700);
  const [record] = await readdir(root);
  assert.ok(record);
  assert.equal((await stat(join(root, record))).mode & 0o777, 0o600);
});

test('default message reference directory honours BRIDGE_MESSAGE_REFERENCE_DIR', async (t) => {
  const previous = process.env.BRIDGE_MESSAGE_REFERENCE_DIR;
  const override = await mkdtemp(join(tmpdir(), 'yeoman-refdir-env-'));
  t.after(() => {
    if (previous === undefined) delete process.env.BRIDGE_MESSAGE_REFERENCE_DIR;
    else process.env.BRIDGE_MESSAGE_REFERENCE_DIR = previous;
  });

  // Without the override a bare WhatsAppClient writes into the live runtime store: an
  // earlier bridge run left six synthetic records there for the test chat.
  delete process.env.BRIDGE_MESSAGE_REFERENCE_DIR;
  const fallback = defaultMessageReferenceDir();
  assert.equal(fallback, join(process.env.HOME ?? '', '.yeoman', 'data', 'bridge', 'whatsapp-message-references'));

  process.env.BRIDGE_MESSAGE_REFERENCE_DIR = override;
  assert.equal(defaultMessageReferenceDir(), override);

  // An empty value means "unset" and must not resolve to the working directory.
  process.env.BRIDGE_MESSAGE_REFERENCE_DIR = '';
  assert.equal(defaultMessageReferenceDir(), fallback);
});
