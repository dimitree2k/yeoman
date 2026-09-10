import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { PROTOCOL_VERSION } from './protocol.js';

// The gateway refuses to start when the on-disk manifest disagrees with the
// protocol version it speaks ("Bridge manifest protocol mismatch"), so a
// protocol bump that forgets the manifest takes the live service down.
test('bridge manifest declares the protocol version the code speaks', () => {
  const root = join(dirname(fileURLToPath(import.meta.url)), '..');
  const manifest = JSON.parse(
    readFileSync(join(root, 'bridge.manifest.json'), 'utf8'),
  ) as { bridgeVersion?: string; protocolVersion?: number; buildId?: string };

  assert.equal(manifest.protocolVersion, PROTOCOL_VERSION);
  assert.ok(manifest.bridgeVersion, 'manifest needs bridgeVersion');
  assert.ok(manifest.buildId, 'manifest needs buildId');
});
