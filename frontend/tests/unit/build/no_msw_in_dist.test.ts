// @vitest-environment node
/**
 * PAT-APP-MAN-02: la construcción de producción no incluye MSW ni los manejadores generados, ni
 * siquiera con `VITE_API_MODE=mock` (el arranque en modo simulado depende de
 * `import.meta.env.DEV`). La prueba ejecuta `vite build` con la configuración real en una carpeta
 * temporal y busca las huellas en toda la salida.
 */
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

import { afterAll, describe, expect, it } from 'vitest';

const ROOT = fileURLToPath(new URL('../../..', import.meta.url));
const VITE = join(ROOT, 'node_modules', 'vite', 'bin', 'vite.js');

/** Huellas de MSW y de los manejadores generados. */
const TRACES = [
  /\bmsw\b/i,
  /mockServiceWorker/i,
  /x-vigia-mock-operation/,
  /live_health_live_get/,
  /\[MSW\]/,
];

/** Activos de texto del paquete; los precomprimidos (`.gz`, `.br`) son binarios y derivan de ellos. */
const TEXT_ASSET = /\.(?:js|mjs|cjs|css|html|json|txt|svg|xml|webmanifest|map)$/i;

function files(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) =>
    entry.isDirectory() ? files(join(directory, entry.name)) : [join(directory, entry.name)],
  );
}

/**
 * `archivo: huella` de cada huella encontrada en `directory`.
 *
 * Solo se miran los activos de texto: un flujo comprimido (`.gz`, `.br`) es binario y puede
 * contener la secuencia de una huella por azar, lo que marcaba un paquete limpio. Como el
 * precomprimido se deriva del activo de texto, mirar el de texto conserva la comprobación.
 */
function traces(directory: string): string[] {
  return files(directory)
    .filter((file) => TEXT_ASSET.test(file))
    .flatMap((file) => {
      const content = readFileSync(file, 'latin1');
      return TRACES.filter((trace) => trace.test(content)).map(
        (trace) => `${relative(directory, file)}: ${String(trace)}`,
      );
    });
}

const directories: string[] = [];

function temporary(prefix: string): string {
  const directory = mkdtempSync(join(tmpdir(), prefix));
  directories.push(directory);
  return directory;
}

afterAll(() => {
  for (const directory of directories) rmSync(directory, { recursive: true, force: true });
});

describe('dist/ sin MSW (PAT-APP-MAN-02)', () => {
  it('el detector encuentra las huellas (control positivo)', () => {
    const directory = temporary('vigia-traces-');
    writeFileSync(
      join(directory, 'a.js'),
      'navigator.serviceWorker.register("/mockServiceWorker.js")',
    );
    writeFileSync(join(directory, 'b.js'), 'import{http as e}from"msw";');
    writeFileSync(join(directory, 'c.js'), 'const ok = 1;');
    expect(traces(directory)).toEqual(['a.js: /mockServiceWorker/i', 'b.js: /\\bmsw\\b/i']);
  });

  it.each(['mock', 'real', ''])(
    'npm run build con VITE_API_MODE=%j no contiene msw ni mockServiceWorker',
    (mode) => {
      const outDir = temporary('vigia-dist-');
      // La orden de `npm run build` en otro proceso: dentro de vitest `NODE_ENV=test` haría que
      // Vite construyera con `import.meta.env.DEV = true`.
      const env: Record<string, string | undefined> = { ...process.env, VITE_API_MODE: mode };
      delete env.NODE_ENV;
      const result = spawnSync(
        process.execPath,
        [VITE, 'build', '--outDir', outDir, '--emptyOutDir'],
        {
          cwd: ROOT,
          env,
          encoding: 'utf8',
        },
      );
      expect(result.status, result.stderr).toBe(0);
      const output = files(outDir).map((file) => relative(outDir, file));
      expect(output).toContain('index.html');
      expect(output.some((file) => file.endsWith('.js'))).toBe(true);
      expect(traces(outDir)).toEqual([]);
    },
    120_000,
  );
});
