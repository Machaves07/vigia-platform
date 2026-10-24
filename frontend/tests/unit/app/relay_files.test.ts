// @vitest-environment node
/**
 * Cada grupo de rutas tiene su `src/pages/scrNN/<grupo>/index.tsx` **en git**, no solo en disco:
 * un `.gitignore` que lo oculte (`coverage/` ocultó `scr03/coverage`) rompe la compilación de un
 * clon limpio aunque el workspace siga en verde.
 */
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import { SCREENS } from '@/app/screens';

const root = fileURLToPath(new URL('../../..', import.meta.url));

const pageDir = (screenId: string): string => `scr${screenId.slice('SCR-'.length)}`;

describe('relevos de los grupos de rutas', () => {
  it('cada grupo tiene su index.tsx seguido por git y no ignorado', () => {
    const tracked = spawnSync('git', ['ls-files', 'src/pages'], { cwd: root, encoding: 'utf8' });
    expect(tracked.status).toBe(0);
    const files = new Set(tracked.stdout.split('\n'));
    const expected = SCREENS.flatMap((screen) =>
      screen.route_groups.map(
        (group) => `src/pages/${pageDir(screen.screen_id)}/${group.id}/index.tsx`,
      ),
    );
    expect(expected).toHaveLength(46);
    expect(expected.filter((file) => !files.has(file))).toEqual([]);
    const ignored = spawnSync('git', ['check-ignore', '--no-index', ...expected], {
      cwd: root,
      encoding: 'utf8',
    });
    expect(ignored.stdout).toBe('');
  });
});
