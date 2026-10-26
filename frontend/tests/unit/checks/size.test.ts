// @vitest-environment node
/**
 * `scripts/checks/size.mjs` (NFR-APP-01): sobre un `dist/` sintético con la forma de la salida de
 * Vite (guion de `index.html`, `modulepreload`, `import()` con `__vite__mapDeps` y CSS), mide el
 * paquete inicial y cada porción, imprime la tabla y falla si una porción supera su límite: con
 * los límites de producción (150 KB) y con un umbral reducido.
 */
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { afterAll, describe, expect, it } from 'vitest';

const ROOT = fileURLToPath(new URL('../../..', import.meta.url));
const CHECK = join(ROOT, 'scripts', 'checks', 'size.mjs');

const temporary = mkdtempSync(join(tmpdir(), 'vigia-size-'));
afterAll(() => {
  rmSync(temporary, { recursive: true, force: true });
});

/** Texto que gzip apenas comprime (cadena de SHA-256 en base64: misma salida siempre). */
function noise(length: number, seed: number): string {
  const parts: string[] = [];
  let size = 0;
  for (let index = 0; size < length; index += 1) {
    const part = createHash('sha256')
      .update(`${String(seed)}:${String(index)}`)
      .digest('base64');
    parts.push(part.replace(/[+/=]/g, ''));
    size += part.length;
  }
  return parts.join('').slice(0, length);
}

interface Shape {
  readonly mainPad?: number;
  readonly screenPad?: number;
  readonly sharedPad?: number;
  readonly libPad?: number;
  readonly withScreen?: boolean;
  readonly missing?: string;
}

let counter = 0;
function dist(shape: Shape = {}): string {
  const { mainPad = 1000, screenPad = 1000, sharedPad = 1000, libPad = 10 } = shape;
  const { withScreen = true } = shape;
  counter += 1;
  const directory = join(temporary, String(counter));
  const assets = join(directory, 'assets');
  mkdirSync(assets, { recursive: true });
  const files: Record<string, string> = {
    'index.html':
      '<!doctype html><html><head>' +
      '<link rel="modulepreload" href="/assets/main-BBBBBBBB.js" crossorigin="">' +
      '<script type="module" crossorigin src="/assets/index-AAAAAAAA.js"></script>' +
      '</head><body><div id="vigia-app"></div></body></html>',
    'assets/index-AAAAAAAA.js':
      'const __vite__mapDeps=(i,m=__vite__mapDeps,d=(m.f||(m.f=["assets/main-BBBBBBBB.js",' +
      '"assets/main-CCCCCCCC.css"])))=>i.map(i=>d[i]);\n' +
      'const s=(f,d)=>f();\n' +
      's(()=>import(`./main-BBBBBBBB.js`),__vite__mapDeps([0,1]));\n',
    'assets/main-BBBBBBBB.js':
      'import{a as b}from"./lib-DDDDDDDD.js";\n' +
      (withScreen
        ? 'const __vite__mapDeps=(i,m=__vite__mapDeps,d=(m.f||(m.f=["assets/scr02-EEEEEEEE.js",' +
          '"assets/shared-GGGGGGGG.js","assets/scr02-FFFFFFFF.css"])))=>i.map(i=>d[i]);\n' +
          'const s=(f,d)=>f();\n' +
          'export const load=()=>s(()=>import(`./scr02-EEEEEEEE.js`).then(e=>e.r),' +
          '__vite__mapDeps([0,1,2]));\n'
        : 'export const load=()=>import("./later-HHHHHHHH.js");\n') +
      `export const pad="${noise(mainPad, 1)}";\n`,
    'assets/main-CCCCCCCC.css': 'body{margin:0}',
    'assets/lib-DDDDDDDD.js': `export const a="${noise(libPad, 4)}";\n`,
    'assets/scr02-EEEEEEEE.js':
      'import{x}from"./shared-GGGGGGGG.js";\nimport{a}from"./lib-DDDDDDDD.js";\n' +
      `export const r="${noise(screenPad, 2)}";\n`,
    'assets/scr02-FFFFFFFF.css': '.scr02{color:inherit}',
    'assets/shared-GGGGGGGG.js': `export const x="${noise(sharedPad, 3)}";\n`,
    'assets/later-HHHHHHHH.js': 'export const later=1;\n',
  };
  for (const [name, content] of Object.entries(files)) {
    if (name !== shape.missing) writeFileSync(join(directory, name), content);
  }
  return directory;
}

function run(directory: string, ...options: string[]) {
  const summary = join(temporary, `summary-${String(counter)}.md`);
  const result = spawnSync(process.execPath, [CHECK, directory, ...options], {
    encoding: 'utf8',
    env: { ...process.env, GITHUB_STEP_SUMMARY: summary },
  });
  return { ...result, summary };
}

function row(stdout: string, name: string): string[] {
  const line = stdout.split('\n').find((text) => text.startsWith(`| ${name} |`));
  expect(line, `fila ${name}`).toBeDefined();
  return (line ?? '').split('|').map((cell) => cell.trim());
}

describe('size.mjs', () => {
  it('pasa con porciones pequeñas e imprime la tabla por porción en la salida y el resumen', () => {
    const result = run(dist());
    expect(result.stderr).toBe('');
    expect(result.status).toBe(0);
    // Inicial: index, main (modulepreload), su CSS (precarga de import() entre iniciales) y lib.
    expect(row(result.stdout, 'paquete inicial')[3]).toBe('4');
    // Porción de scr02: su JavaScript, el compartido que precarga Vite y su CSS.
    const screen = row(result.stdout, 'scr02');
    expect(screen[2]).toBe('scr02');
    expect(screen[3]).toBe('3');
    expect(screen[8]).toBe('cabe');
    expect(result.stdout).toContain('cumplido: 2 porciones');
    expect(readFileSync(result.summary, 'utf8')).toContain('| scr02 | scr02 | 3 |');
  });

  it('falla con el límite de producción si una porción supera 150 KB comprimidos', () => {
    const result = run(dist({ screenPad: 230_000 }));
    expect(result.status).toBe(1);
    expect(Number(row(result.stdout, 'scr02')[5])).toBeGreaterThan(150);
    expect(row(result.stdout, 'scr02')[8]).toBe('SUPERA');
    expect(row(result.stdout, 'paquete inicial')[8]).toBe('cabe');
    expect(result.stderr).toContain('superado en 1: scr02');
  });

  it('falla con un umbral reducido y cuenta lo que precarga Vite con la pantalla', () => {
    // El compartido solo llega por __vite__mapDeps: si no se contara, la porción cabría.
    const directory = dist({ screenPad: 100, sharedPad: 20_000 });
    expect(run(directory, '--chunk-kb', '20').status).toBe(0);
    const result = run(directory, '--chunk-kb', '10');
    expect(result.status).toBe(1);
    expect(row(result.stdout, 'scr02')[8]).toBe('SUPERA');
  });

  it('falla si el paquete inicial supera su límite', () => {
    const result = run(dist({ mainPad: 400_000 }));
    expect(result.status).toBe(1);
    expect(row(result.stdout, 'paquete inicial')[8]).toBe('SUPERA');
    expect(result.stderr).toContain('superado en 1: paquete inicial');
  });

  it('la porción descuenta lo que ya trae el paquete inicial', () => {
    // scr02 también importa lib, que es inicial: pesa en el paquete inicial, no en la porción.
    const result = run(dist({ libPad: 230_000 }), '--initial-kb', '1000');
    expect(result.stderr).toBe('');
    expect(result.status).toBe(0);
    expect(Number(row(result.stdout, 'paquete inicial')[5])).toBeGreaterThan(150);
    expect(Number(row(result.stdout, 'scr02')[5])).toBeLessThan(5);
    expect(row(result.stdout, 'scr02')[3]).toBe('3');
  });

  it.each([
    ['sin dist', { missing: 'index.html' }, 'ejecuta antes «npm run build»'],
    ['una referencia que no existe', { missing: 'assets/shared-GGGGGGGG.js' }, 'no existe'],
    ['sin ninguna porción de pantalla', { withScreen: false }, 'ninguna porción de pantalla'],
  ] as const)('falla cerrado %s', (_case, shape, message) => {
    const result = run(dist(shape));
    expect(result.status).toBe(2);
    expect(result.stderr).toContain(message);
  });

  it('rechaza una opción desconocida o un límite que no es positivo', () => {
    expect(run(dist(), '--chunk', '1').status).toBe(2);
    expect(run(dist(), '--chunk-kb', '0').status).toBe(2);
    expect(run(dist(), '--initial-kb', 'x').status).toBe(2);
  });
});
