/**
 * BR-APP-62 (LC-APP-14): cada página `src/pages/scrNN/**` importa de `src/ports/` solo su puerto
 * (`src/ports/scrNN*`) y `src/ports/shared/*`; nunca el de otra pantalla, `ports.base`, los
 * errores, la validación ni el cliente generado. Con cero páginas, pasa.
 */
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, posix, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../../..');
const PAGES = join(ROOT, 'src/pages');

const SPECIFIERS = [
  /\bimport\s+(?:type\s+)?(?:[^'";]*?\s+from\s+)?['"]([^'"]+)['"]/g,
  /\bexport\s+(?:type\s+)?[^'";]*?\s+from\s+['"]([^'"]+)['"]/g,
  /\bimport\s*\(\s*['"]([^'"]+)['"]\s*\)/g,
];

function specifiers(source: string): string[] {
  return SPECIFIERS.flatMap((pattern) =>
    [...source.matchAll(pattern)].map((match) => match[1] ?? ''),
  );
}

/** Ruta relativa a la raíz de `frontend/` de un especificador, o `null` si es un paquete. */
function target(file: string, specifier: string): string | null {
  if (specifier.startsWith('@/')) return posix.join('src', specifier.slice(2));
  if (specifier.startsWith('.')) {
    return relative(ROOT, resolve(dirname(join(ROOT, file)), specifier))
      .split('\\')
      .join('/');
  }
  if (specifier.startsWith('/src/')) return specifier.slice(1);
  return null;
}

/** Importaciones de `file` (relativo a `frontend/`) que BR-APP-62 prohíbe. */
function violations(file: string, source: string): string[] {
  const screen = /^src\/pages\/(scr\d{2})(?:\/|$)/.exec(file)?.[1];
  if (screen === undefined) return [];
  const own = new RegExp(`^src/ports/${screen}(?![0-9])`);
  return specifiers(source).filter((specifier) => {
    const path = target(file, specifier);
    if (path === null) return false;
    if (path === 'src/client' || path.startsWith('src/client/')) return true;
    if (path !== 'src/ports' && !path.startsWith('src/ports/')) return false;
    return !(own.test(path) || path === 'src/ports/shared' || path.startsWith('src/ports/shared/'));
  });
}

function sourceFiles(dir: string): string[] {
  if (!existsSync(dir)) return [];
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return sourceFiles(path);
    return /\.(?:ts|tsx|js|jsx|mts|mjs)$/.test(name) ? [path] : [];
  });
}

describe('BR-APP-62: cada página solo usa su puerto', () => {
  it('ninguna página de src/pages/scrNN importa el puerto de otra pantalla', () => {
    const found = sourceFiles(PAGES).flatMap((path) => {
      const file = relative(ROOT, path).split('\\').join('/');
      return violations(file, readFileSync(path, 'utf8')).map(
        (specifier) => `${file} → ${specifier}`,
      );
    });
    expect(found).toEqual([]);
  });

  it('el detector distingue el puerto propio, el compartido y los ajenos', () => {
    const page = 'src/pages/scr08/inbox/index.tsx';
    const source = [
      "import { inboxPort } from '@/ports/scr08Inbox';",
      "import type { X } from '../../../ports/scr08/types';",
      "import { isPortError } from '@/ports/shared';",
      "import { sessionPort } from '@/ports/shared/session';",
      "import { Chip } from '@/components/GateStateChip/GateStateChip';",
      "import { useQuery } from '@tanstack/react-query';",
      "import { accountPort } from '@/ports/scr01Account';",
      "import { other } from '../../../ports/scr080Fake';",
      "export { base } from '@/ports/base';",
      "const lazy = () => import('@/ports/errors/map');",
      "import { OPERATIONS } from '@/client';",
      "import '@/ports/validation';",
    ].join('\n');
    expect(violations(page, source)).toEqual([
      '@/ports/scr01Account',
      '../../../ports/scr080Fake',
      '@/client',
      '@/ports/validation',
      '@/ports/base',
      '@/ports/errors/map',
    ]);
    expect(violations('src/components/Thing.tsx', source)).toEqual([]);
  });
});
