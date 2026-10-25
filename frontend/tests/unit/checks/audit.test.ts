// @vitest-environment node
/**
 * `scripts/audit.mjs` (NFR-APP-25): sobre informes sintéticos de `npm audit --json`, un aviso
 * alto o crítico bloquea salvo que todas sus rutas pasen por `@lhci/cli`; uno que llega también
 * por otra dependencia directa bloquea; los moderados no; la excepción vence.
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

interface Vulnerability {
  readonly severity: string;
  readonly isDirect: boolean;
  readonly effects: readonly string[];
}

function vulnerability(severity: string, isDirect: boolean, effects: string[] = []) {
  return { severity, isDirect, effects, via: [{ title: `aviso ${severity}` }] };
}

/** Árbol real de VIG-277: `tmp` llega por `@lhci/cli` directamente y por `external-editor`. */
const LHCI: Record<string, Vulnerability> = {
  '@lhci/cli': vulnerability('high', true),
  tmp: vulnerability('high', false, ['@lhci/cli', 'external-editor']),
  'external-editor': vulnerability('low', false, ['inquirer']),
  inquirer: vulnerability('low', false, ['@lhci/cli']),
  uuid: vulnerability('moderate', false, ['@lhci/cli']),
};

let counter = 0;
function run(vulnerabilities: Record<string, Vulnerability>, today = '2026-10-25') {
  counter += 1;
  const input = join(temporary, `${String(counter)}.json`);
  writeFileSync(input, JSON.stringify({ auditReportVersion: 2, vulnerabilities }));
  return spawnSync(process.execPath, [AUDIT, '--input', input, '--today', today], {
    encoding: 'utf8',
  });
}

describe('audit.mjs', () => {
  it('pasa sin avisos', () => {
    const result = run({});
    expect(result.status).toBe(0);
    expect(result.stdout).toContain('ningún aviso alto o crítico');
  });

  it('exceptúa los avisos altos que solo llegan por @lhci/cli y los lista', () => {
    const result = run(LHCI);
    expect(result.stderr).toBe('');
    expect(result.status).toBe(0);
    expect(result.stdout).toContain('- tmp (high) por @lhci/cli: aviso high');
    expect(result.stdout).toContain('exceptuados (2)');
  });

  it.each(['high', 'critical'])('bloquea un aviso %s de otra dependencia directa', (severity) => {
    const result = run({ ...LHCI, vite: vulnerability(severity, true) });
    expect(result.status).toBe(1);
    expect(result.stderr).toContain(`- vite (${severity}) por vite`);
  });

  it('bloquea un aviso que llega por @lhci/cli y también por otra dependencia', () => {
    const result = run({
      ...LHCI,
      tmp: vulnerability('high', false, ['@lhci/cli', 'vitest']),
      vitest: vulnerability('high', true),
    });
    expect(result.status).toBe(1);
    expect(result.stderr).toContain('- tmp (high) por @lhci/cli, vitest');
  });

  it('bloquea si una ruta no se puede seguir hasta una dependencia directa', () => {
    const result = run({ tmp: vulnerability('high', false, ['desconocido']) });
    expect(result.status).toBe(1);
    expect(result.stderr).toContain('desconocido (ruta desconocida)');
  });

  it('no bloquea avisos moderados ni bajos de ninguna dependencia', () => {
    const result = run({
      vite: vulnerability('moderate', true),
      react: vulnerability('low', true),
    });
    expect(result.status).toBe(0);
  });

  it('bloquea cuando la excepción en uso pasó su fecha de revisión', () => {
    expect(run(LHCI, '2026-12-31').status).toBe(0);
    const result = run(LHCI, '2027-01-01');
    expect(result.status).toBe(1);
    expect(result.stderr).toContain('excepción de @lhci/cli vencida el 2026-12-31');
  });

  it('termina en 2 si el informe no es de npm audit', () => {
    const input = join(temporary, 'error.json');
    writeFileSync(input, JSON.stringify({ error: { code: 'ENOLOCK' } }));
    const result = spawnSync(process.execPath, [AUDIT, '--input', input], { encoding: 'utf8' });
    expect(result.status).toBe(2);
    expect(result.stderr).toContain('no devolvió un informe');
  });
});
