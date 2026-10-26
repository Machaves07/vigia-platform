// @vitest-environment node
/**
 * `scripts/checks/network.mjs` (PR-APP-28 estática, VIG-336): pasa con `src/` real y falla con un
 * componente que hace `fetch('https://ejemplo.invalid')`, con una API de red fuera de `ports/`
 * (también otra que `fetch` en la lista cerrada), con un acceso con corchetes a un objeto global,
 * con un `fetch` de la lista cerrada hacia un destino que no es una ruta literal, y con una URL
 * nueva o relativa al protocolo en la salida construida.
 */
import { spawnSync } from 'node:child_process';
import { cpSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { afterAll, describe, expect, it } from 'vitest';

const ROOT = fileURLToPath(new URL('../../..', import.meta.url));
const CHECK = join(ROOT, 'scripts', 'checks', 'network.mjs');

const temporary = mkdtempSync(join(tmpdir(), 'vigia-network-'));
afterAll(() => {
  rmSync(temporary, { recursive: true, force: true });
});

let counter = 0;
/** Carpetas `src/` y `dist/` nuevas con `files` (ruta relativa → contenido). */
function tree(
  files: Readonly<Record<string, string>>,
  base?: string,
): { src: string; dist: string } {
  counter += 1;
  const directory = join(temporary, String(counter));
  const src = join(directory, 'src');
  const dist = join(directory, 'dist');
  mkdirSync(src, { recursive: true });
  mkdirSync(dist, { recursive: true });
  if (base !== undefined) cpSync(base, src, { recursive: true });
  for (const [path, content] of Object.entries(files)) {
    const target = join(directory, path);
    mkdirSync(join(target, '..'), { recursive: true });
    writeFileSync(target, content);
  }
  return { src, dist };
}

function run(folders?: { src: string; dist: string }) {
  const args = folders === undefined ? [CHECK] : [CHECK, folders.src, folders.dist];
  return spawnSync(process.execPath, args, { encoding: 'utf8' });
}

const COMPONENT = `export function Ejemplo() {
  void fetch('https://ejemplo.invalid');
  return null;
}
`;

describe('network.mjs', () => {
  it('pasa con src/ real (y dist/, si existe)', () => {
    const result = run();
    expect(result.stderr).toBe('');
    expect(result.status).toBe(0);
    expect(result.stdout).toContain('Destinos de red:');
  });

  it('pasa con una copia de src/ real sin dist/', () => {
    const folders = tree({}, join(ROOT, 'src'));
    rmSync(folders.dist, { recursive: true });
    const result = run(folders);
    expect(result.stderr).toBe('');
    expect(result.status).toBe(0);
  });

  it("falla con un componente que hace fetch('https://ejemplo.invalid')", () => {
    const result = run(tree({ 'src/components/Ejemplo/Ejemplo.tsx': COMPONENT }));
    expect(result.status).toBe(1);
    expect(result.stderr).toContain('«https://ejemplo.invalid»');
    expect(result.stderr).toContain('«fetch» fuera de src/ports/');
  });

  it('falla con la URL dentro de un puerto y con una relativa al protocolo', () => {
    const result = run(
      tree({
        'src/ports/scrXX.ts': "export const destino = 'wss://ejemplo.invalid/canal';\n",
        'src/pages/scrXX/index.tsx': 'export const cdn = `//cdn.ejemplo.invalid/x.js`;\n',
      }),
    );
    expect(result.status).toBe(1);
    expect(result.stderr).toContain('«wss://ejemplo.invalid/canal»');
    expect(result.stderr).toContain('«//cdn.ejemplo.invalid/x.js»');
  });

  it.each([
    ['XMLHttpRequest', 'export const x = new XMLHttpRequest();\n'],
    ['WebSocket', 'export const x = (url: string) => new WebSocket(url);\n'],
    ['sendBeacon', "export const x = () => navigator.sendBeacon('/uso', '');\n"],
    ['fetch', 'export const x = globalThis.fetch;\n'],
  ])('falla con %s fuera de src/ports/', (api, content) => {
    const result = run(tree({ 'src/state/telemetria.ts': content }));
    expect(result.status).toBe(1);
    expect(result.stderr).toContain(`«${api}» fuera de src/ports/`);
  });

  it('no cuenta los tipos ni los comentarios', () => {
    const result = run(
      tree({
        'src/state/tipos.ts':
          '// https://ejemplo.invalid en un comentario\n' +
          'export interface Opciones { readonly fetch: typeof fetch }\n',
      }),
    );
    expect(result.stderr).toBe('');
    expect(result.status).toBe(0);
  });

  it('falla si un archivo de la lista cerrada pide un destino que no es una ruta literal', () => {
    const result = run(
      tree({
        'src/app/version.ts':
          'export const leer = (destino: string) => fetch(new URL(destino, location.origin));\n',
      }),
    );
    expect(result.status).toBe(1);
    expect(result.stderr).toContain('src/app/version.ts: «fetch» sin una ruta literal');
  });

  it('el adaptador de la sonda solo puede reenviar sus parámetros', () => {
    const adapter = run(
      tree({
        'src/app/bootstrap.ts':
          'export const adaptar = (input: RequestInfo, init?: RequestInit) => globalThis.fetch(input, init);\n',
      }),
    );
    expect(adapter.stderr).toBe('');
    expect(adapter.status).toBe(0);

    const result = run(
      tree({
        'src/app/bootstrap.ts':
          "export const pedir = (init?: RequestInit) => globalThis.fetch(new URL('/uso', location.origin), init);\n",
      }),
    );
    expect(result.status).toBe(1);
    expect(result.stderr).toContain('no solo reenvía los parámetros del adaptador');
  });

  it('la lista cerrada solo admite fetch: otra API de red falla también allí', () => {
    const result = run(
      tree({
        'src/state/connection/probe.ts':
          "export const sonda = (origen: string) => new WebSocket(new URL('/canal', origen));\n",
      }),
    );
    expect(result.status).toBe(1);
    expect(result.stderr).toContain(
      'src/state/connection/probe.ts: «WebSocket» fuera de src/ports/',
    );
  });

  it.each([
    ["globalThis['fe' + 'tch']('/x')", 'acceso calculado a «globalThis»'],
    ['window[nombre]', 'acceso calculado a «window»'],
    ["self['WebSocket']", '«WebSocket»'],
  ])('falla con el acceso con corchetes %s fuera de src/ports/', (expression, problem) => {
    const result = run(
      tree({
        'src/state/connection/probe.ts': `const nombre = 'x';\nexport const x = () => ${expression};\n`,
      }),
    );
    expect(result.status).toBe(1);
    expect(result.stderr).toContain(
      `src/state/connection/probe.ts: ${problem} fuera de src/ports/`,
    );
  });

  it('admite el acceso con corchetes con una clave literal que no es de red', () => {
    const result = run(
      tree({ 'src/state/tema.ts': "export const x = () => window['matchMedia']('(x)');\n" }),
    );
    expect(result.stderr).toBe('');
    expect(result.status).toBe(0);
  });

  it('falla si la detección de capacidades llama a fetch', () => {
    const result = run(tree({ 'src/bootstrap/check.ts': "export const x = () => fetch('/x');\n" }));
    expect(result.status).toBe(1);
    expect(result.stderr).toContain('src/bootstrap/check.ts: llama a «fetch»');
  });

  it('falla con una URL nueva o relativa al protocolo en dist/', () => {
    const result = run(
      tree({
        'dist/assets/index-abc.js':
          'const a=`http://www.w3.org/2000/svg`;fetch(`https://ejemplo.invalid/x`);\n',
        'dist/index.html': '<script src="//cdn.ejemplo.invalid/a.js"></script>\n',
      }),
    );
    expect(result.status).toBe(1);
    expect(result.stderr).toContain(
      'dist/assets/index-abc.js: destino de red «https://ejemplo.invalid/x»',
    );
    expect(result.stderr).toContain('dist/index.html: destino de red «//cdn.ejemplo.invalid/a.js»');
    expect(result.stderr).not.toContain('w3.org');
  });
});
