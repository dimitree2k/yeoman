import { join } from 'node:path';
import { yeomanHome } from './runtime_paths.js';

export function defaultMediaDir(): string {
  return join(yeomanHome(), 'var', 'media');
}
