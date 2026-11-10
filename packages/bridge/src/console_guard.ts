/**
 * Keep Signal key material out of the bridge log.
 *
 * libsignal (inside Baileys) prints whole session records with console.info when it
 * closes or prunes a session ("Closing session:", "Removing old closed session:").
 * Those records hold private keys, root keys and chain keys as Buffers, and the bridge
 * log is a plain file. Any console argument that carries binary data is replaced by a
 * short marker; the message text itself stays, so the event is still visible.
 */

export const REDACTED_BINARY = '[redacted: object with binary key material]';

const MAX_DEPTH = 6;

function holdsBinary(value: unknown, depth: number, seen: Set<object>): boolean {
  if (value === null || typeof value !== 'object') return false;
  if (value instanceof Uint8Array || value instanceof ArrayBuffer) return true;
  if (ArrayBuffer.isView(value)) return true;
  if (depth >= MAX_DEPTH || seen.has(value)) return false;
  seen.add(value);
  const items = value instanceof Map ? [...value.values()] : Object.values(value);
  return items.some((item) => holdsBinary(item, depth + 1, seen));
}

export function redactConsoleArgs(args: unknown[]): unknown[] {
  return args.map((arg) => {
    if (arg instanceof Error) return arg;
    return holdsBinary(arg, 0, new Set()) ? REDACTED_BINARY : arg;
  });
}

const METHODS = ['log', 'info', 'warn', 'error', 'debug', 'trace'] as const;

export function installConsoleGuard(target: Console = console): void {
  const marker = Symbol.for('yeoman.bridge.consoleGuard');
  const holder = target as unknown as Record<symbol, boolean>;
  if (holder[marker]) return;
  holder[marker] = true;
  for (const method of METHODS) {
    const original = target[method].bind(target);
    target[method] = (...args: unknown[]) => original(...redactConsoleArgs(args));
  }
}
