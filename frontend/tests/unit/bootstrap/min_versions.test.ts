// @vitest-environment node
/**
 * `src/bootstrap/minVersions.generated.ts` (LC-APP-01, NFR-APP-31): es exactamente lo que deriva
 * `browserslist` de `package.json`, el archivo versionado no se edita a mano y la política se
 * resuelve de forma determinista. Es la comprobación que `generate:check` hace para lo generado
 * desde `app.yaml`.
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import { minVersionsDocument, preloadTag, APP_ENTRY } from '../../../config/bootstrap.ts';
import {
  browserPolicyQueries,
  formatMinVersions,
  MIN_ENGINES,
  MIN_VERSIONS_PATH,
  minVersionsFrom,
  parseBrowserEntry,
  renderMinVersions,
  resolveBrowserPolicy,
  type MinVersions,
} from '../../../config/minVersions.ts';
import { MIN_BROWSER_VERSIONS, BROWSER_POLICY_QUERIES } from '@/bootstrap/minVersions.generated';

const ROOT = fileURLToPath(new URL('../../..', import.meta.url));
const PACKAGE_JSON = readFileSync(join(ROOT, 'package.json'), 'utf8');
const PRETTIER = join(ROOT, '.prettierrc.json');

describe('derivación de la versión mínima', () => {
  it('toma la menor versión de cada motor', () => {
    const versions = minVersionsFrom([
      'chrome 156',
      'chrome 155',
      'edge 154',
      'firefox 157.0',
      'firefox 157',
      'safari 26.6',
    ]);
    expect(versions).toEqual({
      chrome: { major: 155, minor: 0 },
      edge: { major: 154, minor: 0 },
      firefox: { major: 157, minor: 0 },
      safari: { major: 26, minor: 6 },
    });
  });

  it('la menor es por versión, no por orden de llegada', () => {
    const versions: MinVersions = minVersionsFrom([
      'safari 26.6',
      'safari 26.10',
      'chrome 155',
      'edge 154',
      'firefox 157',
    ]);
    expect(versions.safari).toEqual({ major: 26, minor: 6 });
  });

  it('lee cada entrada de browserslist', () => {
    expect(parseBrowserEntry('chrome 155')).toEqual({
      engine: 'chrome',
      version: { major: 155, minor: 0 },
    });
    expect(parseBrowserEntry(' safari 26.6 ')).toEqual({
      engine: 'safari',
      version: { major: 26, minor: 6 },
    });
  });

  it('detiene la construcción ante una familia fuera de la política o una entrada ilegible', () => {
    expect(() => parseBrowserEntry('opera 120')).toThrow('opera');
    expect(() => parseBrowserEntry('and_chr 156')).toThrow('and_chr');
    expect(() => parseBrowserEntry('chrome')).toThrow('chrome');
    expect(() => minVersionsFrom(['chrome 155'])).toThrow('edge');
  });

  it('exige la lista browserslist de package.json', () => {
    expect(browserPolicyQueries(PACKAGE_JSON)).toEqual([...BROWSER_POLICY_QUERIES]);
    expect(() => browserPolicyQueries('{}')).toThrow('browserslist');
    expect(() => browserPolicyQueries('{"browserslist": []}')).toThrow('browserslist');
    expect(() => browserPolicyQueries('{"browserslist": [7]}')).toThrow('browserslist');
  });

  it('resuelve la política con el caniuse-lite instalado, de forma determinista', () => {
    const queries = browserPolicyQueries(PACKAGE_JSON);
    const first = resolveBrowserPolicy(queries, ROOT);
    expect(first).toEqual(resolveBrowserPolicy(queries, ROOT));
    expect(first.length).toBeGreaterThan(0);
    expect(() => resolveBrowserPolicy(['navegador inventado'], ROOT)).toThrow(
      'Unknown browser query',
    );
    expect(() => resolveBrowserPolicy(['chrome 155', 'not chrome 155'], ROOT)).toThrow(
      'no cubre ningún navegador',
    );
  });
});

describe('el archivo generado', () => {
  it('es exactamente lo que deriva browserslist y lo formatea Prettier', async () => {
    const expected = await minVersionsDocument(ROOT);
    expect(readFileSync(join(ROOT, MIN_VERSIONS_PATH), 'utf8')).toBe(expected);
  }, 30_000);

  it('declara los cuatro motores de la política con mayor y menor numéricos', () => {
    expect(Object.keys(MIN_BROWSER_VERSIONS).sort()).toEqual([...MIN_ENGINES].sort());
    for (const engine of MIN_ENGINES) {
      const [major, minor] = MIN_BROWSER_VERSIONS[engine];
      expect(Number.isSafeInteger(major), engine).toBe(true);
      expect(Number.isSafeInteger(minor), engine).toBe(true);
    }
  });

  it('el texto sin formatear es reproducible y Prettier no lo cambia dos veces', async () => {
    const versions = minVersionsFrom(resolveBrowserPolicy([...BROWSER_POLICY_QUERIES], ROOT));
    const raw = renderMinVersions(versions, [...BROWSER_POLICY_QUERIES]);
    expect(renderMinVersions(versions, [...BROWSER_POLICY_QUERIES])).toBe(raw);
    const formatted = await formatMinVersions(raw, PRETTIER);
    expect(await formatMinVersions(formatted, PRETTIER)).toBe(formatted);
  }, 30_000);
});

describe('complemento de construcción', () => {
  it('la porción del paquete inicial es el trozo dinámico que lo contiene', () => {
    expect(
      preloadTag({
        'assets/index-abc.js': { type: 'chunk', fileName: 'assets/index-abc.js' },
        'assets/main-abc.js': {
          type: 'chunk',
          fileName: 'assets/main-abc.js',
          isDynamicEntry: true,
          facadeModuleId: `/repo/frontend/${APP_ENTRY}`,
          moduleIds: [`/repo/frontend/${APP_ENTRY}`],
        },
        'assets/index-abc.css': { type: 'asset', fileName: 'assets/index-abc.css' },
      }),
    ).toEqual({
      tag: 'link',
      attrs: { rel: 'modulepreload', href: '/assets/main-abc.js', crossorigin: '' },
      injectTo: 'head-prepend',
    });
  });

  it('sin la porción del paquete inicial no hay etiqueta que inyectar', () => {
    expect(
      preloadTag({ 'assets/index-abc.js': { type: 'chunk', fileName: 'assets/index-abc.js' } }),
    ).toBeNull();
  });

  it('reconoce el módulo por su ruta en Windows y por moduleIds sin facadeModuleId', () => {
    expect(
      preloadTag({
        'assets/main-abc.js': {
          type: 'chunk',
          fileName: 'assets/main-abc.js',
          moduleIds: [`C:\\repo\\frontend\\${APP_ENTRY.replace('/', '\\')}`],
        },
      })?.attrs.href,
    ).toBe('/assets/main-abc.js');
  });
});
