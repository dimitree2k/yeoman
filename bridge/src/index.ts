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

function parseBoolEnv(value: string | undefined, fallback: boolean): boolean {
  if (!value) return fallback;
  const normalized = value.trim().toLowerCase();
  if (['1', 'true', 'yes', 'on'].includes(normalized)) return true;
  if (['0', 'false', 'no', 'off'].includes(normalized)) return false;
  return fallback;
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
const MEDIA_DIR = process.env.MEDIA_DIR || join(homedir(), '.nanobot', 'media');
const MEDIA_INCOMING_DIR = process.env.MEDIA_INCOMING_DIR || join(MEDIA_DIR, 'incoming', 'whatsapp');
const MEDIA_OUTGOING_DIR = process.env.MEDIA_OUTGOING_DIR || join(MEDIA_DIR, 'outgoing', 'whatsapp');
const PERSIST_INBOUND_AUDIO = parseBoolEnv(process.env.WHATSAPP_PERSIST_INBOUND_AUDIO, false);
const ACCEPT_FROM_ME = parseBoolEnv(process.env.WHATSAPP_ACCEPT_FROM_ME, false);
const BRIDGE_TOKEN = (process.env.BRIDGE_TOKEN || '').trim();
const MANIFEST_PATH = process.env.BRIDGE_MANIFEST_PATH || join(process.cwd(), 'bridge.manifest.json');
const READ_RECEIPTS = parseBoolEnv(process.env.WHATSAPP_READ_RECEIPTS, true);

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
console.log(`mediaIncomingDir=${MEDIA_INCOMING_DIR} mediaOutgoingDir=${MEDIA_OUTGOING_DIR}`);
console.log(`persistInboundAudio=${PERSIST_INBOUND_AUDIO}`);
console.log(`acceptFromMe=${ACCEPT_FROM_ME}`);

const identity = loadManifestIdentity(MANIFEST_PATH);
const server = new BridgeServer(
  HOST,
  PORT,
  AUTH_DIR,
  MEDIA_INCOMING_DIR,
  MEDIA_OUTGOING_DIR,
  PERSIST_INBOUND_AUDIO,
  ACCEPT_FROM_ME,
  BRIDGE_TOKEN,
  identity.bridgeVersion,
  identity.buildId,
  READ_RECEIPTS,
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
