// @vitest-environment node
/**
 * Construcción para la imagen (VIG-288, LC-APP-29; NFR-APP-14, 24; R-APP-7; PAT-APP-MAN-06):
 * `version.json` con los patrones que valida `StaticSite`, `.br` y `.gz` junto a cada recurso,
 * mapas de fuente fuera de `dist/`, marcador `vigia-app` obligatorio y `VITE_API_PROXY`.
 *
 * Las pruebas de salida ejecutan `vite build` con la configuración real en carpetas temporales.
 */
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readdirSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { brotliDecompressSync, gunzipSync } from 'node:zlib';

import type { UserConfig } from 'vite';
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';

import {
  APP_MARKER,
  assertAppMarker,
  builtAt,
  contractTagFrom,
  DEV_API_TARGET,
  isPrecompressed,
  isSourceMap,
  parseApiProxy,
  resolveAppVersion,
  versionDocument,
} from '../../../config/build';
import { vigiaRelease } from '../../../config/release';

const ROOT = fileURLToPath(new URL('../../..', import.meta.url));
const VITE = join(ROOT, 'node_modules', 'vite', 'bin', 'vite.js');
const SHA = '0123456789abcdef0123456789abcdef01234567';
const EPOCH = '1792800000';
const EPOCH_ISO = '2026-10-24T00:00:00.000Z';

const directories: string[] = [];

function temporary(prefix: string): string {
  const directory = mkdtempSync(join(tmpdir(), prefix));
  directories.push(directory);
  return directory;
}

afterAll(() => {
  for (const directory of directories) rmSync(directory, { recursive: true, force: true });
});

function walk(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) =>
    entry.isDirectory() ? walk(join(directory, entry.name)) : [join(directory, entry.name)],
  );
}

/** Archivos bajo `directory`, relativos y con `/`, ordenados. */
function files(directory: string): string[] {
  return walk(directory)
    .map((file) => relative(directory, file).split('\\').join('/'))
    .sort();
}

interface Build {
  readonly status: number | null;
  readonly stderr: string;
  readonly outDir: string;
  readonly sourcemapDir: string;
}

function build(env: Record<string, string>): Build {
  const outDir = temporary('vigia-dist-');
  const sourcemapDir = temporary('vigia-maps-');
  // `vite build` en otro proceso: dentro de vitest `NODE_ENV=test` cambiaría la construcción.
  const childEnv: Record<string, string | undefined> = {
    ...process.env,
    VIGIA_SOURCEMAPS_DIR: sourcemapDir,
    ...env,
  };
  delete childEnv.NODE_ENV;
  const result = spawnSync(process.execPath, [VITE, 'build', '--outDir', outDir, '--emptyOutDir'], {
    cwd: ROOT,
    env: childEnv,
    encoding: 'utf8',
  });
  return { status: result.status, stderr: result.stderr, outDir, sourcemapDir };
}

describe('reglas de la release (config/build.ts)', () => {
  it('VITE_APP_VERSION: etiqueta o sha; sin valor, dev; otra forma detiene', () => {
    expect(resolveAppVersion(undefined)).toBe('dev');
    expect(resolveAppVersion('')).toBe('dev');
    expect(resolveAppVersion('v1.4.0')).toBe('v1.4.0');
    expect(resolveAppVersion(SHA)).toBe(SHA);
    expect(resolveAppVersion('a'.repeat(64))).toBe('a'.repeat(64));
    for (const value of ['a'.repeat(65), '-v1', '.v1', 'v1 2', 'v1/2', 'v1\n', 'é']) {
      expect(() => resolveAppVersion(value), value).toThrow('VITE_APP_VERSION');
    }
  });

  it('contract_tag sale de la etiqueta fijada de @vigia/contracts', () => {
    expect(contractTagFrom('git+ssh://git@github.com/Machaves07/vigia-contracts.git#v1.1.0')).toBe(
      'v1.1.0',
    );
    const pinned = JSON.parse(readFileSync(join(ROOT, 'package.json'), 'utf8')) as {
      dependencies: Record<string, string>;
    };
    expect(contractTagFrom(pinned.dependencies['@vigia/contracts'])).toMatch(/^v\d+\.\d+\.\d+$/);
    for (const spec of [
      undefined,
      '',
      'git+ssh://git@github.com/x/vigia-contracts.git',
      'git+ssh://x.git#',
      'git+ssh://x.git#v1#v2',
      'git+ssh://x.git#semver:^1.0.0',
      '1.1.0',
    ]) {
      expect(() => contractTagFrom(spec), String(spec)).toThrow('@vigia/contracts');
    }
  });

  it('built_at: ISO 8601 UTC con milisegundos; SOURCE_DATE_EPOCH lo fija', () => {
    const now = { now: () => Date.parse('2026-10-24T01:02:03.456Z') };
    expect(builtAt(undefined, now)).toBe('2026-10-24T01:02:03.456Z');
    expect(builtAt('', now)).toBe('2026-10-24T01:02:03.456Z');
    expect(builtAt(EPOCH, now)).toBe(EPOCH_ISO);
    expect(builtAt('0', now)).toBe('1970-01-01T00:00:00.000Z');
    for (const value of ['-1', '1.5', 'abc', '123456789012', ' 1']) {
      expect(() => builtAt(value, now), value).toThrow('SOURCE_DATE_EPOCH');
    }
  });

  it('version.json tiene exactamente app_version, contract_tag y built_at', () => {
    const release = { app_version: SHA, contract_tag: 'v1.1.0', built_at: EPOCH_ISO };
    expect(JSON.parse(versionDocument({ ...release, extra: 'x' } as typeof release))).toEqual(
      release,
    );
    expect(() => versionDocument({ ...release, app_version: '' })).toThrow('app_version');
    expect(() => versionDocument({ ...release, contract_tag: 'v 1' })).toThrow('contract_tag');
    expect(() => versionDocument({ ...release, built_at: '2026-10-23T08:00:00+00:00' })).toThrow(
      'built_at',
    );
  });

  it('el marcador vigia-app es obligatorio en index.html', () => {
    expect(APP_MARKER).toBe('vigia-app');
    expect(() => {
      assertAppMarker('<div id="vigia-app"></div>');
    }).not.toThrow();
    expect(() => {
      assertAppMarker('<div id="vigia"></div>');
    }).toThrow('vigia-app');
    expect(() => {
      assertAppMarker('');
    }).toThrow('vigia-app');
  });

  it.each([
    ['assets/index-abc.js', true, false],
    ['assets/index-abc.css', true, false],
    ['assets/fonts/a-1.woff2', true, false],
    ['assets/index-abc.js.map', false, true],
    ['assets/index-abc.CSS.MAP', false, true],
    ['assets/index-abc.js.br', false, false],
    ['assets/index-abc.js.gz', false, false],
    ['index.html', false, false],
    ['version.json', false, false],
  ])('%s → precomprimido %s, mapa %s', (fileName, precompressed, sourceMap) => {
    expect(isPrecompressed(fileName)).toBe(precompressed);
    expect(isSourceMap(fileName)).toBe(sourceMap);
  });

  it('VITE_API_PROXY: un origen http(s); sin valor, vigia-api de make run', () => {
    expect(parseApiProxy(undefined)).toBe(DEV_API_TARGET);
    expect(parseApiProxy('')).toBe(DEV_API_TARGET);
    expect(parseApiProxy('http://host.docker.internal:8000')).toBe(
      'http://host.docker.internal:8000',
    );
    expect(parseApiProxy('https://api.local/')).toBe('https://api.local');
    for (const value of [
      'host.docker.internal:8000',
      'ftp://x',
      'http://u:p@x:8000',
      'http://x:8000/api',
      'http://x:8000?a=1',
      'http://x:8000#a',
      'no es una url',
    ]) {
      expect(() => parseApiProxy(value), value).toThrow('VITE_API_PROXY');
    }
  });
});

describe('vite.config.ts', () => {
  async function resolved(env: Record<string, string>): Promise<UserConfig> {
    for (const [key, value] of Object.entries(env)) vi.stubEnv(key, value);
    const { default: config } = await import('../../../vite.config');
    if (typeof config !== 'function') throw new Error('vite.config.ts exporta una función');
    return config({ command: 'build', mode: 'production' });
  }

  it('incrusta app_version como import.meta.env.VITE_APP_VERSION', async () => {
    const config = await resolved({ VITE_APP_VERSION: SHA });
    expect(config.define).toEqual({ 'import.meta.env.VITE_APP_VERSION': JSON.stringify(SHA) });
    expect(config.build?.sourcemap).toBe('hidden');
    const names = (config.plugins ?? [])
      .flat()
      .map((plugin) =>
        plugin !== null && typeof plugin === 'object' && 'name' in plugin ? plugin.name : null,
      );
    expect(names).toContain('vigia-release');
  });

  it('sin VITE_APP_VERSION incrusta dev', async () => {
    const config = await resolved({ VITE_APP_VERSION: '' });
    expect(config.define).toEqual({ 'import.meta.env.VITE_APP_VERSION': '"dev"' });
  });
});

describe('complemento vigia-release', () => {
  function run(indexSource: string | Uint8Array): string[] {
    const plugin = vigiaRelease({
      release: { app_version: 'dev', contract_tag: 'v1.1.0', built_at: EPOCH_ISO },
      sourcemapDir: temporary('vigia-maps-'),
    });
    const hook = plugin.generateBundle as unknown as {
      order: string;
      handler: (this: unknown, options: unknown, bundle: Record<string, unknown>) => void;
    };
    expect(hook.order).toBe('post');
    const emitted: string[] = [];
    const context = {
      error(message: string): never {
        throw new Error(message);
      },
      emitFile(file: { fileName: string }) {
        emitted.push(file.fileName);
        return file.fileName;
      },
    };
    hook.handler.call(
      context,
      {},
      {
        'index.html': { type: 'asset', fileName: 'index.html', source: indexSource },
        'assets/a-1.js': { type: 'chunk', fileName: 'assets/a-1.js', code: 'export {};' },
      },
    );
    return emitted.sort();
  }

  it('sin el marcador en index.html la construcción falla y no emite nada', () => {
    expect(() => run('<div id="app"></div>')).toThrow('vigia-app');
    expect(() => run(new TextEncoder().encode('<div></div>'))).toThrow('vigia-app');
  });

  it('con el marcador emite .br, .gz y version.json', () => {
    expect(run('<div id="vigia-app"></div>')).toEqual([
      'assets/a-1.js.br',
      'assets/a-1.js.gz',
      'version.json',
    ]);
    expect(run(new TextEncoder().encode('<div id="vigia-app"></div>'))).toHaveLength(3);
  });

  it('una versión inválida detiene la configuración', () => {
    expect(() =>
      vigiaRelease({
        release: { app_version: 'v 1', contract_tag: 'v1.1.0', built_at: EPOCH_ISO },
        sourcemapDir: temporary('vigia-maps-'),
      }),
    ).toThrow('app_version');
  });

  it.each(['', 'frontend-sourcemaps', './maps'])(
    'una carpeta de mapas relativa (%j) detiene la configuración',
    (sourcemapDir) => {
      expect(() =>
        vigiaRelease({
          release: { app_version: 'dev', contract_tag: 'v1.1.0', built_at: EPOCH_ISO },
          sourcemapDir,
        }),
      ).toThrow('absoluta');
    },
  );
});

describe('salida de npm run build', () => {
  let output: Build;

  beforeAll(() => {
    output = build({ VITE_APP_VERSION: SHA, SOURCE_DATE_EPOCH: EPOCH, VITE_API_MODE: '' });
  }, 120_000);

  it('construye', () => {
    expect(output.status, output.stderr).toBe(0);
  });

  it('version.json lleva la versión, la etiqueta del contrato y la hora de la construcción', () => {
    const raw = readFileSync(join(output.outDir, 'version.json'), 'utf8');
    expect(JSON.parse(raw)).toEqual({
      app_version: SHA,
      contract_tag: 'v1.1.0',
      built_at: EPOCH_ISO,
    });
  });

  it('cada recurso de assets/ tiene .br y .gz que se descomprimen en el mismo contenido', () => {
    const all = files(output.outDir);
    const assets = all.filter((file) => file.startsWith('assets/') && isPrecompressed(file));
    expect(assets.some((file) => file.endsWith('.js'))).toBe(true);
    expect(assets.some((file) => file.endsWith('.css'))).toBe(true);
    for (const asset of assets) {
      const original = readFileSync(join(output.outDir, asset));
      const br = readFileSync(join(output.outDir, `${asset}.br`));
      const gz = readFileSync(join(output.outDir, `${asset}.gz`));
      expect(brotliDecompressSync(br).equals(original), asset).toBe(true);
      expect(gunzipSync(gz).equals(original), asset).toBe(true);
    }
  });

  it('dist/ solo tiene index.html, version.json y assets/; sin mapas ni trabajador de servicio', () => {
    const all = files(output.outDir);
    expect(all.filter((file) => !file.startsWith('assets/'))).toEqual([
      'index.html',
      'version.json',
    ]);
    expect(all.filter((file) => isSourceMap(file))).toEqual([]);
    for (const file of all.filter((name) => /\.(js|css)$/.test(name))) {
      expect(readFileSync(join(output.outDir, file), 'utf8'), file).not.toContain(
        'sourceMappingURL',
      );
    }
    expect(readFileSync(join(output.outDir, 'index.html'), 'utf8')).toContain('vigia-app');
  });

  it('los mapas de fuente quedan fuera de dist/', () => {
    const maps = files(output.sourcemapDir);
    expect(maps.length).toBeGreaterThan(0);
    expect(maps.every((file) => file.startsWith('assets/') && file.endsWith('.js.map'))).toBe(true);
  });

  it('un VITE_APP_VERSION inválido detiene la construcción', () => {
    const failed = build({ VITE_APP_VERSION: 'v 1' });
    expect(failed.status).not.toBe(0);
    expect(failed.stderr).toContain('VITE_APP_VERSION');
  }, 120_000);
});
