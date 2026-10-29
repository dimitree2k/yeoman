import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash, randomBytes } from 'node:crypto';
import { mkdtemp, readFile, rm, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { aesEncryptGCM, hmacSign } from '@whiskeysockets/baileys';
import { proto } from '@whiskeysockets/baileys/WAProto/index.js';

import { POLL_REFERENCE_TTL_MS, PollReferenceStore } from './poll_reference_store.js';
import { WhatsAppClient } from './whatsapp.js';

const CHAT = '123@g.us';
const POLL = 'POLLID';
const ME_PN = '4900@s.whatsapp.net';
const ME_LID = '777@lid';
const ALICE_LID = '111@lid';
const ALICE_PN = '4911@s.whatsapp.net';
const BOB_LID = '222@lid';
const OPTIONS = ['Freitag', 'Samstag', 'Sonntag'];

/** Encrypt a vote the way a WhatsApp client does, so decryption is really exercised. */
function encryptVote(secret: Buffer, creator: string, voter: string, choices: string[], pollId = POLL) {
  const sign = Buffer.concat([
    Buffer.from(pollId), Buffer.from(creator), Buffer.from(voter), Buffer.from('Poll Vote'),
    new Uint8Array([1]),
  ]);
  const key0 = hmacSign(secret, new Uint8Array(32), 'sha256');
  const decKey = hmacSign(sign, key0, 'sha256');
  const iv = randomBytes(12);
  const plain = proto.Message.PollVoteMessage.encode({
    selectedOptions: choices.map((name) => createHash('sha256').update(Buffer.from(name)).digest()),
  }).finish();
  const encPayload = aesEncryptGCM(plain, decKey, iv, Buffer.from(`${pollId}\u0000${voter}`));
  return { encPayload, encIv: iv };
}

async function setup(t: any) {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-polls-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const path = join(root, 'ops', 'bridge-poll-references.json');
  const store = new PollReferenceStore(path);
  const secret = randomBytes(32);
  await store.remember({ chatJid: CHAT, pollId: POLL, secret, options: OPTIONS }, 1_000);
  return { store, secret, path };
}

function vote(secret: Buffer, voter: string, choices: string[], atMs: number, signedAs = voter) {
  return {
    chatJid: CHAT, pollId: POLL, ...encryptVote(secret, ME_LID, signedAs, choices),
    voterJids: [voter, ALICE_PN], creatorJids: [ME_PN, ME_LID], senderTimestampMs: atMs,
  };
}

test('votes are decrypted, latest per voter wins, results are counts only', async (t) => {
  const { store, secret, path } = await setup(t);

  assert.equal(await store.recordVote(vote(secret, ALICE_LID, ['Freitag'], 10), 2_000), true);
  assert.equal(await store.recordVote(vote(secret, ALICE_LID, ['Samstag'], 20), 2_000), true);
  // A late-delivered older vote does not replace the newer one.
  assert.equal(await store.recordVote(vote(secret, ALICE_LID, ['Sonntag'], 15), 2_000), true);
  assert.equal(await store.recordVote(vote(secret, BOB_LID, ['Samstag'], 30), 3_000), true);

  assert.deepEqual(await store.results(CHAT, POLL, 3_000), {
    options: [{ name: 'Freitag', count: 0 }, { name: 'Samstag', count: 2 }, { name: 'Sonntag', count: 0 }],
    total_votes: 2,
    updated_at_ms: 3_000,
  });

  // Retracting a vote removes it.
  await store.recordVote(vote(secret, BOB_LID, [], 40), 4_000);
  assert.equal((await store.results(CHAT, POLL, 4_000))!.total_votes, 1);

  const raw = await readFile(path, 'utf8');
  for (const jid of [ALICE_LID, ALICE_PN, BOB_LID, '111', '222']) assert.equal(raw.includes(jid), false);
  assert.equal((await stat(path)).mode & 0o777, 0o600);
});

test('a voter that signed with the phone number form is still matched', async (t) => {
  const { store, secret } = await setup(t);
  assert.equal(await store.recordVote(vote(secret, ALICE_LID, ['Sonntag'], 10, ALICE_PN), 2_000), true);
  assert.equal((await store.results(CHAT, POLL, 2_000))!.options[2].count, 1);
});

test('a vote that does not decrypt, or for another chat or poll, is not counted', async (t) => {
  const { store, secret } = await setup(t);
  const wrongSecret = randomBytes(32);
  assert.equal(await store.recordVote(vote(wrongSecret, ALICE_LID, ['Freitag'], 10), 2_000), false);
  assert.equal(await store.recordVote({ ...vote(secret, ALICE_LID, ['Freitag'], 10), chatJid: '456@g.us' }, 2_000), false);
  assert.equal(await store.recordVote({ ...vote(secret, ALICE_LID, ['Freitag'], 10), pollId: 'OTHER' }, 2_000), false);
  assert.equal((await store.results(CHAT, POLL, 2_000))!.total_votes, 0);
  assert.equal(await store.results('456@g.us', POLL, 2_000), undefined);
});

test('counts survive a restart; forget and expiry delete the reference', async (t) => {
  const { store, secret, path } = await setup(t);
  await store.recordVote(vote(secret, ALICE_LID, ['Freitag'], 10), 2_000);

  const reopened = new PollReferenceStore(path);
  assert.equal((await reopened.results(CHAT, POLL, 2_000))!.options[0].count, 1);

  await reopened.forget(CHAT, POLL, 2_000);
  assert.equal(await new PollReferenceStore(path).results(CHAT, POLL, 2_000), undefined);

  await reopened.remember({ chatJid: CHAT, pollId: 'P2', secret, options: OPTIONS }, 1_000);
  assert.equal(await reopened.results(CHAT, 'P2', 1_000 + POLL_REFERENCE_TTL_MS), undefined);
  assert.equal((await readFile(path, 'utf8')).includes('P2'), false);
});

test('client keeps the poll secret, counts votes from upsert and never forwards them', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'yeoman-poll-client-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const forwarded: unknown[] = [];
  const errors: string[] = [];
  const client = new WhatsAppClient({
    authDir: join(root, 'auth'), messageReferenceDir: join(root, 'references'),
    pollReferenceFile: join(root, 'polls.json'),
    onMessage: (msg) => { forwarded.push(msg); }, onQR: () => {}, onStatus: () => {},
    onError: (e) => { errors.push(e); },
  });
  const secret = randomBytes(32);
  const listeners = new Map<string, (value: unknown) => void>();
  (client as any).sock = {
    sendMessage: async () => ({ key: { id: POLL, remoteJid: CHAT, fromMe: true },
      message: { messageContextInfo: { messageSecret: new Uint8Array(secret) } } }),
    readMessages: async () => undefined,
    ev: { on: (name: string, listener: (value: unknown) => void) => listeners.set(name, listener) },
  };
  (client as any).connected = true;
  (client as any).selfJids = new Set([ME_PN, ME_LID]);
  (client as any).registerInboundMessageHandler();
  (client as any).running = true;
  (client as any).acceptingProviderEvents = true;

  await client.sendPoll({ to: CHAT, question: 'Wann?', options: OPTIONS });
  const encrypted = encryptVote(secret, ME_LID, ALICE_LID, ['Samstag']);
  listeners.get('messages.upsert')!({ type: 'notify', messages: [{
    key: { remoteJid: CHAT, id: 'VOTE1', participant: ALICE_LID, participantAlt: ALICE_PN },
    message: { pollUpdateMessage: {
      pollCreationMessageKey: { remoteJid: CHAT, id: POLL, fromMe: true },
      vote: encrypted, senderTimestampMs: 10,
    } },
  }] });

  let results: any;
  for (let i = 0; i < 50; i += 1) {
    results = await client.lookupPollResults({ chatJid: CHAT, pollId: POLL });
    if (results.status === 'found' && results.results.total_votes === 1) break;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  assert.equal(results.status, 'found');
  assert.deepEqual(results.results.options.map((o: any) => o.count), [0, 1, 0]);
  assert.deepEqual(forwarded, []);
  assert.deepEqual(errors, []);

  assert.deepEqual(await client.lookupPollResults({ chatJid: '456@g.us', pollId: POLL }), { status: 'unknown' });
  assert.equal((await client.lookupPollResults({ chatJid: CHAT, pollId: POLL, forget: true })).status, 'found');
  assert.deepEqual(await client.lookupPollResults({ chatJid: CHAT, pollId: POLL }), { status: 'unknown' });
});
