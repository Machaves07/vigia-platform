// @vitest-environment node
/**
 * La política de navegadores que publica `README.md` (NFR-APP-31, NFR-APP-49) es la de
 * `package.json`: las consultas, en el mismo orden, y las versiones mínimas que resuelve
 * `browserslist` con el `caniuse-lite` instalado. El acta de comisionamiento cita esa sección,
 * así que el texto no puede quedarse atrás de la configuración.
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import {
  browserPolicyQueries,
  MIN_ENGINES,
  minVersionsFrom,
  resolveBrowserPolicy,
  type MinVersions,
} from '../../../config/minVersions.ts';

const ROOT = fileURLToPath(new URL('../../..', import.meta.url));
const PACKAGE_JSON = readFileSync(join(ROOT, 'package.json'), 'utf8');
const README = readFileSync(join(ROOT, 'README.md'), 'utf8');

const SECTION_TITLE = '## Política de navegadores';
const ENGINE_NAMES: Readonly<Record<(typeof MIN_ENGINES)[number], string>> = {
  chrome: 'Chrome',
  edge: 'Edge',
  firefox: 'Firefox',
  safari: 'Safari',
};

/** Texto de la sección hasta el siguiente título de nivel 2. */
function policySection(readme: string): string {
  const start = readme.indexOf(`\n${SECTION_TITLE}\n`);
  if (start === -1) throw new Error(`README.md no tiene la sección «${SECTION_TITLE}»`);
  const body = readme.slice(start + SECTION_TITLE.length + 2);
  const end = body.search(/^## /m);
  return end === -1 ? body : body.slice(0, end);
}

/** Las consultas publicadas: cada elemento de lista con una sola consulta entre comillas. */
function documentedQueries(section: string): string[] {
  return [...section.matchAll(/^- `([^`]+)`$/gm)].map((match) => match[1] ?? '');
}

/** Filas `| Motor | versión | … |` de la tabla de versiones mínimas. */
function documentedMinimums(section: string): Map<string, string> {
  const rows = new Map<string, string>();
  for (const match of section.matchAll(/^\| (Chrome|Edge|Firefox|Safari) +\| ([0-9.]+) +\|/gm)) {
    const [, engine = '', version = ''] = match;
    if (rows.has(engine)) throw new Error(`README.md repite la fila de ${engine}`);
    rows.set(engine, version);
  }
  return rows;
}

function formatVersion({ major, minor }: { major: number; minor: number }): string {
  return minor === 0 ? String(major) : `${String(major)}.${String(minor)}`;
}

function expectedMinimums(versions: MinVersions): Map<string, string> {
  return new Map(
    MIN_ENGINES.map((engine) => [ENGINE_NAMES[engine], formatVersion(versions[engine])]),
  );
}

describe('README.md: política de navegadores (NFR-APP-31)', () => {
  const section = policySection(README);
  const queries = browserPolicyQueries(PACKAGE_JSON);

  it('publica exactamente las consultas de browserslist de package.json, en orden', () => {
    expect(documentedQueries(section)).toEqual([...queries]);
  });

  it('publica la versión mínima que resuelve browserslist para cada motor', () => {
    const resolved = minVersionsFrom(resolveBrowserPolicy(queries, ROOT));
    expect(documentedMinimums(section)).toEqual(expectedMinimums(resolved));
  });

  it('browserslist no resuelve ninguna familia que el README no nombre', () => {
    const families = new Set(
      resolveBrowserPolicy(queries, ROOT).map((entry) => entry.trim().split(' ')[0]),
    );
    expect([...families].sort()).toEqual([...MIN_ENGINES].sort());
  });
});

describe('lectura de la sección', () => {
  it('falla si falta la sección', () => {
    expect(() => policySection('# Título\n\n## Otra\n')).toThrow('Política de navegadores');
  });

  it('se detiene en el siguiente título de nivel 2 y no lee consultas de otras secciones', () => {
    const text = `# T\n\n${SECTION_TITLE}\n\n- \`last 2 Chrome versions\`\n\n## Otra\n\n- \`not IE 11\`\n`;
    expect(documentedQueries(policySection(text))).toEqual(['last 2 Chrome versions']);
  });

  it('rechaza una fila de motor repetida', () => {
    expect(() => documentedMinimums('| Chrome | 155 |\n| Chrome | 154 |\n')).toThrow('Chrome');
  });

  it('una versión con menor distinto de cero se escribe con punto', () => {
    expect(formatVersion({ major: 26, minor: 6 })).toBe('26.6');
    expect(formatVersion({ major: 155, minor: 0 })).toBe('155');
  });
});
