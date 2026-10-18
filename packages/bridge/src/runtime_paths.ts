import { homedir } from 'node:os';
import { join } from 'node:path';

export function yeomanHome(): string {
  const configured = (process.env.YEOMAN_HOME || '').trim();
  return configured || join(homedir(), '.yeoman');
}

export function defaultBridgeAuthDir(): string {
  return join(yeomanHome(), 'whatsapp-auth');
}
