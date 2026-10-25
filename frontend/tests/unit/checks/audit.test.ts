// @vitest-environment node
/**
 * `scripts/audit.mjs` (NFR-APP-25): con un `package-lock.json` y un informe de `npm audit --json`
 * sintéticos, un aviso alto o crítico bloquea salvo que todas sus rutas hacia `package.json`
 * pasen por `@lhci/cli`; uno que llega también por otra dependencia directa bloquea; los
 * moderados no; la excepción vence. Las rutas salen del bloqueo, no de `effects`.
 */
import { spawnSync } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { afterAll, describe, expect, it } from 'vitest';

const ROOT = fileURLToPath(new URL('../../..', import.meta.url));
const AUDIT = join(ROOT, 'scripts', 'audit.mjs');

const temporary = mkdtempSync(join(tmpdir(), 'vigia-audit-'));
afterAll(() => {
  rmSync(temporary, { recursive: true, force: true });
});

type Packages = Record<string, Record<string, unknown>>;

/** Forma del árbol real: `tmp` llega por `@lhci/cli` directamente y por `inquirer`. */
const LOCK: Packages = {
  '': {
    name: 'raiz',
    dependencies: { react: '1' },
    devDependencies: { '@lhci/cli': '1', vitest: '1' },
  },
  'node_modules/@lhci/cli': {
    dev: true,
    dependencies: { '@lhci/utils': '1', inquirer: '1', tmp: '1' },
  },
  'node_modules/@lhci/utils': { dev: true, dependencies: { lighthouse: '1' } },
  'node_modules/lighthouse': { dev: true, dependencies: { '@lhci/utils': '1' } },
  'node_modules/inquirer': { dev: true, dependencies: { 'external-editor': '1' } },
  'node_modules/external-editor': { dev: true, dependencies: { tmp: '1' } },
  'node_modules/tmp': { dev: true },
  'node_modules/vitest': { dev: true, devDependencies: { tmp: '1' } },
  'node_modules/react': {},
};

function vulnerability(severity: string, nodes?: string[]) {
  return {
    severity,
    isDirect: false,
    effects: [],
    via: [{ title: `aviso ${severity}` }],
    ...(nodes === undefined ? {} : { nodes }),
  };
}

const LHCI = {
  '@lhci/cli': vulnerability('high', ['node_modules/@lhci/cli']),
  // npm 10 deja `effects` vacío en `@lhci/utils` (ciclo con lighthouse): no cuenta.
  '@lhci/utils': vulnerability('high', ['node_modules/@lhci/utils']),
  tmp: vulnerability('high', ['node_modules/tmp']),
  'external-editor': vulnerability('low', ['node_modules/external-editor']),
};

let counter = 0;
function run(
  vulnerabilities: Record<string, unknown>,
  { lock = LOCK, today = '2026-10-25' }: { lock?: Packages; today?: string } = {},
) {
  counter += 1;
  const input = join(temporary, `report-${String(counter)}.json`);
  const lockPath = join(temporary, `lock-${String(counter)}.json`);
  writeFileSync(input, JSON.stringify({ auditReportVersion: 2, vulnerabilities }));
  writeFileSync(lockPath, JSON.stringify({ lockfileVersion: 3, packages: lock }));
  return spawnSync(
    process.execPath,
    [AUDIT, '--input', input, '--lock', lockPath, '--today', today],
    { encoding: 'utf8' },
  );
}

describe('audit.mjs', () => {
  it('pasa sin avisos', () => {
    const result = run({});
    expect(result.status).toBe(0);
    expect(result.stdout).toContain('ningún aviso alto o crítico');
  });

  it('exceptúa los avisos altos que solo llegan por @lhci/cli, también en un ciclo', () => {
    const result = run(LHCI);
    expect(result.stderr).toBe('');
    expect(result.status).toBe(0);
    expect(result.stdout).toContain('- tmp (high) por @lhci/cli: aviso high');
    expect(result.stdout).toContain('- @lhci/utils (high) por @lhci/cli');
    expect(result.stdout).toContain('exceptuados (3)');
  });

  it.each(['high', 'critical'])('bloquea un aviso %s de otra dependencia directa', (severity) => {
    const result = run({ ...LHCI, react: vulnerability(severity, ['node_modules/react']) });
    expect(result.status).toBe(1);
    expect(result.stderr).toContain(`- react (${severity}) por react`);
  });

  it('bloquea un aviso que llega por @lhci/cli y también por otra dependencia', () => {
    const lock = { ...LOCK, 'node_modules/vitest': { dev: true, dependencies: { tmp: '1' } } };
    const result = run(LHCI, { lock });
    expect(result.status).toBe(1);
    expect(result.stderr).toContain('- tmp (high) por @lhci/cli, vitest');
  });

  it('sigue la resolución de node_modules: una copia anidada es otra instalación', () => {
    const lock = {
      ...LOCK,
      'node_modules/vitest': { dev: true, dependencies: { tmp: '2' } },
      'node_modules/vitest/node_modules/tmp': { dev: true },
    };
    // La copia de la raíz solo llega por @lhci/cli; la anidada, por vitest.
    expect(run(LHCI, { lock }).status).toBe(0);
    const nested = run(
      { ...LHCI, tmp: vulnerability('high', ['node_modules/vitest/node_modules/tmp']) },
      { lock },
    );
    expect(nested.status).toBe(1);
    expect(nested.stderr).toContain('- tmp (high) por vitest');
  });

  it('sin «nodes» en el informe, revisa todas las instalaciones con ese nombre', () => {
    const lock = {
      ...LOCK,
      'node_modules/vitest': { dev: true, dependencies: { tmp: '2' } },
      'node_modules/vitest/node_modules/tmp': { dev: true },
    };
    const result = run({ ...LHCI, tmp: vulnerability('high') }, { lock });
    expect(result.status).toBe(1);
    expect(result.stderr).toContain('- tmp (high) por @lhci/cli, vitest');
  });

  it('las dependencias de desarrollo de un paquete publicado no son rutas', () => {
    // `vitest` declara `tmp` en devDependencies: npm no la instala por esa vía.
    expect(run(LHCI).status).toBe(0);
  });

  it.each([
    [
      'nadie la requiere',
      { 'node_modules/huerfano': { dev: true } },
      'huerfano (ruta desconocida)',
    ],
    ['no está en el bloqueo', {}, 'huerfano (sin instalación en el bloqueo)'],
  ])('bloquea si la ruta no se puede demostrar: %s', (_case, extra, message) => {
    const result = run({ huerfano: vulnerability('high') }, { lock: { ...LOCK, ...extra } });
    expect(result.status).toBe(1);
    expect(result.stderr).toContain(message);
  });

  it('no bloquea avisos moderados ni bajos de ninguna dependencia', () => {
    const result = run({
      react: vulnerability('moderate', ['node_modules/react']),
      vitest: vulnerability('low', ['node_modules/vitest']),
    });
    expect(result.status).toBe(0);
  });

  it('bloquea cuando la excepción en uso pasó su fecha de revisión', () => {
    expect(run(LHCI, { today: '2026-12-31' }).status).toBe(0);
    const result = run(LHCI, { today: '2027-01-01' });
    expect(result.status).toBe(1);
    expect(result.stderr).toContain('excepción de @lhci/cli vencida el 2026-12-31');
  });

  it('termina en 2 si el informe no es de npm audit o falta el bloqueo', () => {
    const input = join(temporary, 'error.json');
    writeFileSync(input, JSON.stringify({ error: { code: 'ENOLOCK' } }));
    const report = spawnSync(process.execPath, [AUDIT, '--input', input], { encoding: 'utf8' });
    expect(report.status).toBe(2);
    expect(report.stderr).toContain('no devolvió un informe');
    const valid = join(temporary, 'valid.json');
    writeFileSync(valid, JSON.stringify({ vulnerabilities: {} }));
    const lock = spawnSync(
      process.execPath,
      [AUDIT, '--input', valid, '--lock', join(temporary, 'no-existe.json')],
      { encoding: 'utf8' },
    );
    expect(lock.status).toBe(2);
    expect(lock.stderr).toContain('no se pudo leer');
  });
});
