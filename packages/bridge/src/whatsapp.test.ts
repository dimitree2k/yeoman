import test from 'node:test';
import assert from 'node:assert/strict';

import { resolveParticipantJid, shouldIgnoreFromMeInbound } from './whatsapp.js';

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
