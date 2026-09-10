import test from 'node:test';
import assert from 'node:assert/strict';

import { BridgeServer } from './server.js';

test('BridgeServer dispatches delete_message to the WhatsApp client', async () => {
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
  );
  const calls: unknown[] = [];
  const deleted = {
    chatJid: '12345@s.whatsapp.net',
    messageId: 'BAE5EXACTMESSAGEID',
  };

  (server as any).wa = {
    deleteMessage: async (payload: unknown) => {
      calls.push(payload);
      return deleted;
    },
  };

  const result = await (server as any).executeCommand('delete_message', deleted);

  assert.deepEqual(result, { deleted });
  assert.deepEqual(calls, [deleted]);
});
