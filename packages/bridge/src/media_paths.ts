import { homedir } from 'node:os';
import { join } from 'node:path';

export function defaultMediaDir(): string {
  return join(homedir(), '.yeoman', 'var', 'media');
}
