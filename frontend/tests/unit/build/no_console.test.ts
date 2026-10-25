// @vitest-environment node
/**
 * NFR-APP-18 (LC-APP-04): la construcción de producción no escribe en la consola. La prueba
 * ejecuta `vite build` con la configuración real en una carpeta temporal y busca en todo el
 * JavaScript de `dist/` llamadas a `console.log`, `console.debug` y `console.info` (también con
 * corchetes) y la huella del registro de desarrollo de `ports.base` (`[api] …`), que la
 * construcción elimina porque `import.meta.env.DEV` es la constante `false`.
 *
 * `console.error` y `console.warn` de las dependencias (React, React Router) no se cuentan: el
 * armazón les quita la ocasión con `rootErrorOptions` y `ignoreRouterError`
 * (`tests/components/global_error_handler.test.tsx`).
 */
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

import { afterAll, describe, expect, it } from 'vitest';

const ROOT = fileURLToPath(new URL('../../..', import.meta.url));
const VITE = join(ROOT, 'node_modules', 'vite', 'bin', 'vite.js');

/** Escrituras prohibidas en la consola y la huella del registro de desarrollo. */
const TRACES = [
  /\bconsole\s*\.\s*(?:log|debug|info)\b/,
  /\bconsole\s*\[\s*["'`](?:log|debug|info)["'`]\s*\]/,
  /\[api\] /,
];

/** JavaScript del paquete; los precomprimidos (`.gz`, `.br`) son binarios y derivan de él. */
const SCRIPT = /\.(?:js|mjs|cjs|html)$/i;

function files(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) =>
    entry.isDirectory() ? files(join(directory, entry.name)) : [join(directory, entry.name)],
  );
}

/** `archivo: huella` de cada huella encontrada en `directory`. */
function traces(directory: string): string[] {
  return files(directory)
    .filter((file) => SCRIPT.test(file))
    .flatMap((file) => {
      const content = readFileSync(file, 'utf8');
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

describe('dist/ sin consola (NFR-APP-18)', () => {
  it('el detector encuentra cada huella (control positivo)', () => {
    const directory = temporary('vigia-console-');
    writeFileSync(join(directory, 'a.js'), 'console.log("x");');
    writeFileSync(join(directory, 'b.js'), 'console .debug(x)');
    writeFileSync(join(directory, 'c.js'), 'console["info"](x)');
    writeFileSync(join(directory, 'd.js'), 'return`[api] ${e.method}`');
    writeFileSync(join(directory, 'e.js'), 'console.error(x);console.warn(y);const logo=1;');
    expect(traces(directory)).toEqual([
      'a.js: /\\bconsole\\s*\\.\\s*(?:log|debug|info)\\b/',
      'b.js: /\\bconsole\\s*\\.\\s*(?:log|debug|info)\\b/',
      'c.js: /\\bconsole\\s*\\[\\s*["\'`](?:log|debug|info)["\'`]\\s*\\]/',
      'd.js: /\\[api\\] /',
    ]);
  });

  it('npm run build no deja console.log, console.debug, console.info ni el registro de desarrollo', () => {
    const outDir = temporary('vigia-dist-console-');
    // La orden de `npm run build` en otro proceso: dentro de vitest `NODE_ENV=test` haría que
    // Vite construyera con `import.meta.env.DEV = true`.
    const env: Record<string, string | undefined> = { ...process.env };
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
    const scripts = files(outDir).filter((file) => file.endsWith('.js'));
    expect(scripts.length).toBeGreaterThan(0);
    expect(traces(outDir)).toEqual([]);
  }, 120_000);
});
