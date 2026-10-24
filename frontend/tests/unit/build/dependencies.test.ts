// @vitest-environment node
/**
 * Presupuesto de dependencias (NFR-APP-25, `tech-stack-decisions.md` §9): `DEPENDENCIES.md` y
 * `package.json` coinciden por igualdad; cada versión es exacta; cada licencia es de la lista
 * permitida y coincide con la del paquete instalado; producción es exactamente §9.
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

const ROOT = fileURLToPath(new URL('../../..', import.meta.url));

/** Producción de §9, sin más ni menos. */
const BUDGET_PRODUCTION = [
  '@tanstack/react-query',
  '@vigia/contracts',
  'ajv',
  'ajv-formats',
  'date-fns',
  'date-fns-tz',
  'openapi-fetch',
  'react',
  'react-dom',
  'react-router',
  'recharts',
];

/** Desarrollo de §9 y de la lista del alcance de VIG-277. */
const BUDGET_DEVELOPMENT = [
  '@axe-core/playwright',
  '@cyclonedx/cyclonedx-npm',
  '@lhci/cli',
  '@playwright/test',
  '@testing-library/react',
  '@testing-library/user-event',
  '@vitejs/plugin-react',
  'axe-core',
  'eslint',
  'eslint-plugin-jsx-a11y',
  'eslint-plugin-react-hooks',
  'fast-check',
  'jsdom',
  'knip',
  'msw',
  'openapi-typescript',
  'prettier',
  'typescript',
  'typescript-eslint',
  'vite',
  'vitest',
  'vitest-axe',
];

/** Auxiliares cerradas: cada una la exige una dependencia de §9. */
const AUXILIARY = {
  '@testing-library/dom': '@testing-library/react',
  '@types/node': 'typescript',
  '@types/react': 'react',
  '@types/react-dom': 'react-dom',
  '@vitest/coverage-v8': 'vitest',
  // La usa el arranque (VIG-289) para derivar la versión mínima de la política de navegadores
  // (NFR-APP-31); ya está en el árbol de desarrollo por `eslint-plugin-react-hooks`.
  browserslist: 'eslint-plugin-react-hooks',
} as const;

/** Lista permitida (AGENTS.md, NFR-CTR-26) más las excepciones permisivas de A-43. */
const ALLOWED_LICENSES = new Set([
  'MIT',
  'BSD-2-Clause',
  'BSD-3-Clause',
  'Apache-2.0',
  'ISC',
  'PSF-2.0',
  'MPL-2.0',
  'MIT-0',
  '0BSD',
]);
const CONTRACTS = '@vigia/contracts';
const CONTRACTS_URL = 'git+ssh://git@github.com/Machaves07/vigia-contracts.git#v1.1.0';

interface Row {
  readonly name: string;
  readonly kind: string;
  readonly version: string;
  readonly license: string;
  readonly budget: string;
}

interface PackageJson {
  readonly version: string;
  readonly license?: string;
  readonly dependencies?: Record<string, string>;
  readonly devDependencies?: Record<string, string>;
}

function readPackage(path: string): PackageJson {
  return JSON.parse(readFileSync(path, 'utf8')) as PackageJson;
}

const manifest = readPackage(join(ROOT, 'package.json'));
const lock = JSON.parse(readFileSync(join(ROOT, 'package-lock.json'), 'utf8')) as {
  packages: Record<string, { version?: string; resolved?: string }>;
};

function tableRows(markdown: string): Row[] {
  return markdown
    .split('\n')
    .filter((line) => /^\|\s*`/.test(line))
    .map((line) => {
      const cells = line
        .split('|')
        .slice(1, -1)
        .map((cell) => cell.trim());
      const [name = '', kind = '', version = '', license = '', budget = ''] = cells;
      return {
        name: name.replaceAll('`', ''),
        kind,
        version: version.replaceAll('`', ''),
        license,
        budget,
      };
    });
}

const rows = tableRows(readFileSync(join(ROOT, 'DEPENDENCIES.md'), 'utf8'));
const declared = [
  ...Object.entries(manifest.dependencies ?? {}).map(([name, version]) => ({
    name,
    kind: 'producción',
    version,
  })),
  ...Object.entries(manifest.devDependencies ?? {}).map(([name, version]) => ({
    name,
    kind: 'desarrollo',
    version,
  })),
];

describe('DEPENDENCIES.md frente a package.json', () => {
  it('lista exactamente las dependencias de package.json, con su tipo y versión', () => {
    const fromTable = rows.map(({ name, kind, version }) => ({ name, kind, version }));
    const byName = (a: { name: string }, b: { name: string }) => a.name.localeCompare(b.name);
    expect([...fromTable].sort(byName)).toEqual([...declared].sort(byName));
    expect(new Set(rows.map((row) => row.name)).size).toBe(rows.length);
  });

  it('toda versión es exacta y coincide con el lock', () => {
    for (const { name, version } of declared) {
      const locked = lock.packages[`node_modules/${name}`];
      if (name === CONTRACTS) {
        expect(version).toBe(CONTRACTS_URL);
        expect(locked?.resolved).toMatch(
          /^git\+ssh:\/\/git@github\.com\/Machaves07\/vigia-contracts\.git#[0-9a-f]{40}$/,
        );
        continue;
      }
      expect(version, name).toMatch(/^\d+\.\d+\.\d+$/);
      expect(locked?.version, name).toBe(version);
    }
  });

  it('cada licencia es de la lista permitida y la del paquete instalado', () => {
    for (const row of rows) {
      const installed = readPackage(join(ROOT, 'node_modules', row.name, 'package.json'));
      if (row.name === CONTRACTS) {
        expect(row.license).toBe('Propietaria (U-01)');
        expect(installed.license).toBe('SEE LICENSE IN LICENSE');
        expect(installed.version).toBe('1.1.0');
        continue;
      }
      expect(ALLOWED_LICENSES.has(row.license), `${row.name}: ${row.license}`).toBe(true);
      expect(installed.license, row.name).toBe(row.license);
      expect(installed.version, row.name).toBe(row.version);
    }
  });

  it('producción es exactamente la de §9 y desarrollo es §9 más auxiliares cerradas', () => {
    expect(Object.keys(manifest.dependencies ?? {}).sort()).toEqual([...BUDGET_PRODUCTION].sort());
    expect(Object.keys(manifest.devDependencies ?? {}).sort()).toEqual(
      [...BUDGET_DEVELOPMENT, ...Object.keys(AUXILIARY)].sort(),
    );
    for (const row of rows) {
      const auxiliary = Object.hasOwn(AUXILIARY, row.name);
      expect(row.budget, row.name).toBe(auxiliary ? 'auxiliar' : '§9');
      if (auxiliary) expect(row.kind, row.name).toBe('desarrollo');
    }
  });
});
