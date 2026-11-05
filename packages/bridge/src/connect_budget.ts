import { mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';

import { yeomanHome } from './runtime_paths.js';

/**
 * How many times the bridge may try to reach WhatsApp, persisted so that no restart
 * (systemd, Overseer, deploy) refills it. Mirrors yeoman_shared/disconnect_class.py.
 *
 * - needs_human (401 logged out, 403, 411, 500): zero attempts until the owner relinks.
 * - transient: at most `perHour` attempts per hour and `perDay` per 24 h; once the daily
 *   budget is spent, one probe every `probeIntervalMs` and nothing else.
 * - needs_restart (515): reconnect at once, not counted.
 */
export type DisconnectClass = 'transient' | 'needs_restart' | 'needs_human';

const WHATSAPP_CODES: Record<number, [string, DisconnectClass]> = {
  401: ['logged_out', 'needs_human'],
  403: ['forbidden', 'needs_human'],
  408: ['timed_out', 'transient'],
  411: ['multidevice_mismatch', 'needs_human'],
  428: ['connection_closed', 'transient'],
  440: ['connection_replaced', 'transient'],
  500: ['bad_session', 'needs_human'],
  503: ['unavailable_service', 'transient'],
  515: ['restart_required', 'needs_restart'],
};

export function classifyWhatsAppDisconnect(code: number | undefined): { reason: string; cls: DisconnectClass } {
  if (code === undefined) return { reason: 'unknown', cls: 'transient' };
  const hit = WHATSAPP_CODES[code];
  return hit ? { reason: hit[0], cls: hit[1] } : { reason: 'unknown', cls: 'transient' };
}

export interface BudgetHold {
  kind: 'needs_human' | 'budget_exhausted';
  since: number;
  code?: number;
  reason?: string;
  nextProbeAt?: number;
}

interface BudgetFile {
  attempts: number[];
  hold?: BudgetHold;
}

export interface ConnectBudgetOptions {
  perHour?: number;
  perDay?: number;
  probeIntervalMs?: number;
  healthyAfterMs?: number;
  now?: () => number;
}

export type BudgetDecision =
  | { action: 'connect'; probe: boolean }
  | { action: 'wait'; ms: number; why: 'hourly_limit' | 'budget_exhausted' }
  | { action: 'hold'; hold: BudgetHold };

const HOUR_MS = 3_600_000;
const DAY_MS = 24 * HOUR_MS;

export function defaultConnectBudgetFile(): string {
  const override = (process.env.BRIDGE_CONNECT_BUDGET_FILE || '').trim();
  if (override) return override;
  return join(yeomanHome(), 'data', 'ops', 'whatsapp-connect-budget.json');
}

export class ConnectBudget {
  readonly perHour: number;
  readonly perDay: number;
  readonly probeIntervalMs: number;
  readonly healthyAfterMs: number;
  private readonly now: () => number;
  private state: BudgetFile = { attempts: [] };

  constructor(private readonly file: string, opts: ConnectBudgetOptions = {}) {
    this.perHour = opts.perHour ?? 5;
    this.perDay = opts.perDay ?? 15;
    this.probeIntervalMs = opts.probeIntervalMs ?? 8 * HOUR_MS;
    this.healthyAfterMs = opts.healthyAfterMs ?? 10 * 60_000;
    this.now = opts.now ?? Date.now;
    this.load();
  }

  private load(): void {
    try {
      const raw = JSON.parse(readFileSync(this.file, 'utf8')) as Partial<BudgetFile>;
      this.state = {
        attempts: Array.isArray(raw.attempts) ? raw.attempts.filter((t) => typeof t === 'number') : [],
        hold: raw.hold && typeof raw.hold === 'object' ? raw.hold : undefined,
      };
    } catch {
      this.state = { attempts: [] };
    }
  }

  private save(): void {
    mkdirSync(dirname(this.file), { recursive: true });
    const tmp = `${this.file}.${process.pid}.tmp`;
    writeFileSync(tmp, JSON.stringify(this.state, null, 2), { mode: 0o600 });
    renameSync(tmp, this.file);
  }

  get hold(): BudgetHold | undefined {
    return this.state.hold;
  }

  attemptsWithin(windowMs: number): number {
    const cutoff = this.now() - windowMs;
    return this.state.attempts.filter((t) => t > cutoff).length;
  }

  decide(): BudgetDecision {
    const now = this.now();
    const hold = this.state.hold;
    if (hold?.kind === 'needs_human') return { action: 'hold', hold };
    if (hold?.kind === 'budget_exhausted') {
      const at = hold.nextProbeAt ?? now;
      return at <= now ? { action: 'connect', probe: true } : { action: 'wait', ms: at - now, why: 'budget_exhausted' };
    }
    this.state.attempts = this.state.attempts.filter((t) => t > now - DAY_MS);
    if (this.state.attempts.length >= this.perDay) {
      this.state.hold = { kind: 'budget_exhausted', since: now, nextProbeAt: now + this.probeIntervalMs };
      this.save();
      return { action: 'wait', ms: this.probeIntervalMs, why: 'budget_exhausted' };
    }
    const inHour = this.state.attempts.filter((t) => t > now - HOUR_MS).sort((a, b) => a - b);
    if (inHour.length >= this.perHour) {
      return { action: 'wait', ms: Math.max(1_000, inHour[0] + HOUR_MS - now), why: 'hourly_limit' };
    }
    return { action: 'connect', probe: false };
  }

  recordAttempt(probe: boolean): void {
    const now = this.now();
    this.state.attempts.push(now);
    if (probe && this.state.hold?.kind === 'budget_exhausted') {
      this.state.hold = { ...this.state.hold, nextProbeAt: now + this.probeIntervalMs };
    }
    this.save();
  }

  holdNeedsHuman(code: number | undefined, reason: string): BudgetHold {
    const hold: BudgetHold = { kind: 'needs_human', since: this.now(), code, reason };
    this.state.hold = hold;
    this.save();
    return hold;
  }

  /** A connection stayed up for `healthyAfterMs`, or the owner relinked / resumed. */
  reset(): void {
    this.state = { attempts: [] };
    this.save();
  }
}
