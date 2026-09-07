import test from 'node:test';
import assert from 'node:assert/strict';
import { homedir } from 'node:os';
import { join } from 'node:path';

import { defaultMediaDir } from './media_paths.js';

test('bridge defaults media storage under the Yeoman var directory', () => {
  assert.equal(defaultMediaDir(), join(homedir(), '.yeoman', 'var', 'media'));
});
