#!/usr/bin/env node

import { webcrypto } from 'crypto';
import { readFileSync } from 'fs';
import { homedir } from 'os';
import { join } from 'path';

import { BridgeServer } from './server.js';

if (!globalThis.crypto) {
  (globalThis as any).crypto = webcrypto;
}

function resolveLoopbackHost(value: string | undefined): string {
  const raw = (value || '127.0.0.1').trim().toLowerCase();
  if (!raw || raw === 'localhost') return '127.0.0.1';
  if (raw === '::1') return raw;
  if (raw.startsWith('127.')) return raw;
  if (raw.startsWith('::ffff:127.')) return raw;
  throw new Error(`Invalid BRIDGE_HOST="${value}". Only loopback addresses are allowed.`);
}

const PORT = parseInt(process.env.BRIDGE_PORT || '3001', 10);
let HOST = '127.0.0.1';
try {
  HOST = resolveLoopbackHost(process.env.BRIDGE_HOST);
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(1);
}
const AUTH_DIR = process.env.AUTH_DIR || join(homedir(), '.nanobot', 'whatsapp-auth');
const BRIDGE_TOKEN = (process.env.BRIDGE_TOKEN || '').trim();
const MANIFEST_PATH = process.env.BRIDGE_MANIFEST_PATH || join(process.cwd(), 'bridge.manifest.json');

function loadManifestIdentity(path: string): { bridgeVersion: string; buildId: string } {
  try {
    const raw = readFileSync(path, 'utf8');
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    const bridgeVersion = String(parsed.bridgeVersion || '').trim() || 'unknown';
    const buildId = String(parsed.buildId || '').trim() || 'dev';
    return { bridgeVersion, buildId };
  } catch {
    return { bridgeVersion: 'unknown', buildId: 'dev' };
  }
}

if (!BRIDGE_TOKEN) {
  console.error('Missing BRIDGE_TOKEN. Refusing to start insecure bridge.');
  process.exit(1);
}

console.log('nanobot WhatsApp Bridge');
console.log('=======================');
console.log(`host=${HOST} port=${PORT} authDir=${AUTH_DIR}`);

const identity = loadManifestIdentity(MANIFEST_PATH);
const server = new BridgeServer(
  HOST,
  PORT,
  AUTH_DIR,
  BRIDGE_TOKEN,
  identity.bridgeVersion,
  identity.buildId,
);

process.on('SIGINT', async () => {
  await server.stop();
  process.exit(0);
});

process.on('SIGTERM', async () => {
  await server.stop();
  process.exit(0);
});

server.start().catch((error) => {
  console.error('Failed to start bridge:', error);
  process.exit(1);
});
