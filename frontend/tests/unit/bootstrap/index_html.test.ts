/**
 * `index.html` del arranque (LC-APP-01, PAT-APP-REN-01; NFR-APP-19 y 31; criterios 2 y 3 de
 * VIG-289): el marcado no lleva guiones ni estilos en línea —la política de contenido no tiene
 * `unsafe-inline`—, conserva el marcador `vigia-app`, carga la comprobación de capacidades como
 * módulo externo y trae la sección estática de no compatible. Se comprueba sobre el archivo y sobre
 * `dist/index.html`, que además lleva el `modulepreload` del paquete inicial.
 */
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { afterAll, beforeAll, describe, expect, it } from 'vitest';

import { APP_CONTAINER_ID, UNSUPPORTED_SECTION_ID } from '@/bootstrap/check';
import { text } from '@/texts';

/**
 * Raíz de `frontend/`. Sale de `import.meta.url` sin `new URL(…, import.meta.url)`: Vite reescribe
 * esa forma como una URL del servidor de desarrollo, y en jsdom deja de ser `file:`.
 */
const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..');
const VITE = join(ROOT, 'node_modules', 'vite', 'bin', 'vite.js');
const CHECK_SCRIPT = '/src/bootstrap/check.ts';

const outDir = mkdtempSync(join(tmpdir(), 'vigia-bootstrap-'));
let built: string;

beforeAll(() => {
  built = buildIndexHtml();
}, 120_000);

afterAll(() => {
  rmSync(outDir, { recursive: true, force: true });
});

/** Construye la aplicación en una carpeta temporal y devuelve su `index.html`. */
function buildIndexHtml(): string {
  const env: Record<string, string | undefined> = { ...process.env };
  // La orden real de `npm run build`: dentro de vitest `NODE_ENV=test` haría que Vite construyera
  // con `import.meta.env.DEV = true`.
  delete env.NODE_ENV;
  const result = spawnSync(process.execPath, [VITE, 'build', '--outDir', outDir, '--emptyOutDir'], {
    cwd: ROOT,
    env,
    encoding: 'utf8',
  });
  expect(result.status, result.stderr).toBe(0);
  return readFileSync(join(outDir, 'index.html'), 'utf8');
}

function parse(html: string): Document {
  return new DOMParser().parseFromString(html, 'text/html');
}

/** Marcado prohibido por la política de contenido: guiones y estilos en línea (NFR-APP-19). */
function inlineMarkup(document: Document): string[] {
  const problems: string[] = [];
  const scripts = [...document.querySelectorAll('script')];
  for (const script of scripts) {
    if (!script.hasAttribute('src')) problems.push('<script> sin src');
    if (script.textContent.trim() !== '') problems.push('<script> con código en línea');
  }
  if (document.querySelector('style') !== null) problems.push('<style> en línea');
  for (const element of document.querySelectorAll('[style]')) {
    problems.push(`atributo style en <${element.tagName.toLowerCase()}>`);
  }
  for (const element of document.querySelectorAll('*')) {
    for (const attribute of element.attributes) {
      if (/^on/i.test(attribute.name)) problems.push(`manejador en línea ${attribute.name}`);
      if (/^\s*javascript:/i.test(attribute.value)) {
        problems.push(`URL javascript: en ${attribute.name}`);
      }
    }
  }
  return problems;
}

function normalize(textContent: string | null): string {
  return (textContent ?? '').replace(/\s+/g, ' ').trim();
}

describe('index.html sin transformar', () => {
  const document = parse(readFileSync(join(ROOT, 'index.html'), 'utf8'));

  it('no lleva guiones ni estilos en línea', () => {
    expect(inlineMarkup(document)).toEqual([]);
  });

  it('conserva el contenedor vigia-app y carga la comprobación de capacidades como módulo', () => {
    expect(document.getElementById(APP_CONTAINER_ID)).not.toBeNull();
    const scripts = [...document.querySelectorAll('script')];
    expect(scripts).toHaveLength(1);
    expect(scripts[0]?.getAttribute('type')).toBe('module');
    expect(scripts[0]?.getAttribute('src')).toBe(CHECK_SCRIPT);
  });

  it('trae la sección estática de no compatible, oculta y con el texto del catálogo', () => {
    const section = document.getElementById(UNSUPPORTED_SECTION_ID);
    expect(section?.getAttribute('lang')).toBe('es');
    expect(section?.hasAttribute('hidden')).toBe(true);
    expect(section?.querySelector('h1')?.textContent).toBe(text('bootstrap.unsupported_title'));
    expect(
      [...(section?.querySelectorAll('p') ?? [])].map((paragraph) =>
        normalize(paragraph.textContent),
      ),
    ).toEqual([text('bootstrap.unsupported_body')]);
  });
});

describe('dist/index.html construido', () => {
  it('mantiene el marcado sin guiones ni estilos en línea, vigia-app y la sección', () => {
    const document = parse(built);
    expect(inlineMarkup(document)).toEqual([]);
    expect(document.getElementById(APP_CONTAINER_ID)).not.toBeNull();
    expect(document.getElementById(UNSUPPORTED_SECTION_ID)?.hasAttribute('hidden')).toBe(true);
    const scripts = [...document.querySelectorAll('script')];
    expect(scripts).toHaveLength(1);
    expect(scripts[0]?.getAttribute('src')).toMatch(/^\/assets\/index-[A-Za-z0-9_-]+\.js$/);
  });

  it('precarga el paquete inicial, que es la porción que el guion de comprobación importa', () => {
    const document = parse(built);
    const links = [...document.querySelectorAll('link[rel="modulepreload"]')];
    expect(links).toHaveLength(1);
    const href = links[0]?.getAttribute('href') ?? '';
    expect(href).toMatch(/^\/assets\/[A-Za-z0-9_-]+\.js$/);
    const entry = document.querySelector('script')?.getAttribute('src') ?? '';
    expect(href).not.toBe(entry);
    // La porción existe y es la que el guion de comprobación carga de forma dinámica.
    const bundle = readFileSync(join(outDir, href), 'utf8');
    expect(bundle.length).toBeGreaterThan(0);
    expect(readFileSync(join(outDir, entry), 'utf8')).toContain(basename(href));
  });
});
