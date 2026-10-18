import assert from 'node:assert/strict';
import { homedir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import { defaultMediaDir } from './media_paths.js';
import { defaultMessageReferenceDir } from './message_reference_store.js';
import { defaultBridgeOutboxDir } from './outbox.js';
import { defaultBridgeAuthDir, yeomanHome } from './runtime_paths.js';

const defaults = [
  [defaultBridgeAuthDir, 'whatsapp-auth'],
  [defaultMediaDir, 'var/media'],
  [defaultBridgeOutboxDir, 'data/bridge/whatsapp-outbox'],
  [defaultMessageReferenceDir, 'data/ops/bridge-message-references'],
] as const;

test('bridge runtime paths follow YEOMAN_HOME and preserve the legacy root', (t) => {
  const previous = process.env.YEOMAN_HOME;
  const previousReferenceDir = process.env.BRIDGE_MESSAGE_REFERENCE_DIR;
  t.after(() => {
    if (previous === undefined) delete process.env.YEOMAN_HOME;
    else process.env.YEOMAN_HOME = previous;
    if (previousReferenceDir === undefined) delete process.env.BRIDGE_MESSAGE_REFERENCE_DIR;
    else process.env.BRIDGE_MESSAGE_REFERENCE_DIR = previousReferenceDir;
  });

  delete process.env.BRIDGE_MESSAGE_REFERENCE_DIR;
  delete process.env.YEOMAN_HOME;
  assert.equal(yeomanHome(), join(homedir(), '.yeoman'));
  for (const [resolve, suffix] of defaults) {
    assert.equal(resolve(), join(homedir(), '.yeoman', ...suffix.split('/')));
  }

  process.env.YEOMAN_HOME = ' /tmp/yeoman-bridge-home ';
  assert.equal(yeomanHome(), '/tmp/yeoman-bridge-home');
  for (const [resolve, suffix] of defaults) {
    assert.equal(resolve(), join('/tmp/yeoman-bridge-home', ...suffix.split('/')));
  }

  process.env.BRIDGE_MESSAGE_REFERENCE_DIR = '/tmp/yeoman-explicit-references';
  assert.equal(defaultMessageReferenceDir(), '/tmp/yeoman-explicit-references');
});
