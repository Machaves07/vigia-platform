#!/usr/bin/env node
/**
 * Presupuesto de tamaño de la construcción (NFR-APP-01, `[objetivo propio]`; VIG-294): el paquete
 * inicial ≤ 250 KB comprimido y cada porción diferida (una por pantalla `scrNN`, más cualquier
 * otra carga diferida) ≤ 150 KB comprimida. 1 KB = 1000 bytes; la cifra que se compara es gzip
 * de nivel 9 (mayor que brotli: el límite queda del lado prudente). Imprime la tabla por porción
 * y, en GitHub Actions, la añade al resumen del trabajo (`GITHUB_STEP_SUMMARY`).
 *
 * Mide `dist/` tal como sale de `npm run build`, sin construir otra vez:
 *
 * - **Paquete inicial**: lo que nombra `index.html` (guion, `modulepreload` y hojas de estilo),
 *   sus importaciones estáticas y, si un archivo inicial carga con `import()` otro que también es
 *   inicial (el arranque de LC-APP-01 cargando `main`), las dependencias que Vite precarga con él.
 * - **Porción**: cada destino de `import()` que no es inicial, con todo lo que Vite precarga para
 *   esa importación (`__vite__mapDeps`: JavaScript y CSS) y sus importaciones estáticas, menos lo
 *   que ya trae el paquete inicial. Es lo que el navegador descarga al abrir esa pantalla.
 *
 * Las importaciones se leen con el analizador de Vite (`parseAst`), no con expresiones regulares.
 * Falla cerrado: sin `dist/index.html`, con una referencia a un archivo que no existe o sin
 * ninguna porción de pantalla, termina en 2.
 *
 * Uso: `size.mjs [carpeta dist] [--initial-kb N] [--chunk-kb N]` (las pruebas reducen los límites).
 * Termina en 0 si todo cabe, en 1 si algo supera su límite y en 2 si no puede medir.
 *
 * No la ejecuta `npm run check` (`run.mjs` la omite): necesita la construcción y corre después de
 * `npm run build`, en `npm run size` y en el trabajo `frontend` de `ci.yml`.
 */
import { appendFileSync, existsSync, readFileSync } from 'node:fs';
import { basename, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { brotliCompressSync, constants, gzipSync } from 'node:zlib';

import { parseAst } from 'vite';

const root = fileURLToPath(new URL('../..', import.meta.url));

/**
 * @param {string[]} argv
 * @returns {{ dist: string, initialKb: number, chunkKb: number }}
 */
function parseArguments(argv) {
  let dist = join(root, 'dist');
  let initialKb = 250;
  let chunkKb = 150;
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index] ?? '';
    if (argument === '--initial-kb' || argument === '--chunk-kb') {
      const value = Number(argv[index + 1]);
      if (!Number.isFinite(value) || value <= 0) fail(`${argument} necesita un número positivo.`);
      if (argument === '--initial-kb') initialKb = value;
      else chunkKb = value;
      index += 1;
    } else if (argument.startsWith('--')) {
      fail(`Opción desconocida: ${argument}`);
    } else {
      dist = resolve(argument);
    }
  }
  return { dist, initialKb, chunkKb };
}

/**
 * @param {string} message
 * @returns {never}
 */
function fail(message) {
  process.stderr.write(`size.mjs: ${message}\n`);
  process.exit(2);
}

const { dist, initialKb, chunkKb } = parseArguments(process.argv.slice(2));
const indexHtml = join(dist, 'index.html');
if (!existsSync(indexHtml)) fail(`no existe ${indexHtml}: ejecuta antes «npm run build».`);

/**
 * Ruta de `dist/` de una referencia (`/assets/x.js`, `assets/x.js` o `./x.js` desde `assets/`).
 * @param {string} reference
 * @param {string} from carpeta (relativa a `dist/`) del archivo que la contiene
 */
function distPath(reference, from) {
  const clean = reference.split(/[?#]/, 1)[0] ?? '';
  if (clean.startsWith('/')) return clean.slice(1);
  if (clean.startsWith('./') || clean.startsWith('../')) {
    return join(from, clean).split('\\').join('/');
  }
  return clean;
}

/** @param {string} file ruta relativa a `dist/` */
function mustExist(file) {
  if (!existsSync(join(dist, file))) fail(`la referencia «${file}» no existe en ${dist}.`);
  return file;
}

// --- Raíces del paquete inicial (index.html) ---------------------------------------------------

const html = readFileSync(indexHtml, 'utf8');
const initialRoots = new Set();
for (const tag of html.match(/<(?:script|link)\b[^>]*>/gi) ?? []) {
  const source = /\b(?:src|href)\s*=\s*["']([^"']+)["']/i.exec(tag)?.[1];
  if (source === undefined || /^[a-z]+:|^\/\//i.test(source)) continue;
  const isScript = /^<script/i.test(tag);
  const rel = /\brel\s*=\s*["']([^"']+)["']/i.exec(tag)?.[1] ?? '';
  if (isScript || /\b(?:modulepreload|stylesheet)\b/i.test(rel)) {
    initialRoots.add(mustExist(distPath(source, '')));
  }
}
if (initialRoots.size === 0) fail('index.html no carga ningún recurso propio.');

// --- Grafo de importaciones de cada archivo JavaScript -----------------------------------------

/**
 * @typedef {{ statics: string[], dynamics: Map<string, string[]> }} Imports
 * `dynamics`: destino de cada `import()` → lo que Vite precarga con él (vacío si nada).
 */

/** @type {Map<string, Imports>} */
const graph = new Map();

/**
 * @param {unknown} value
 * @returns {value is Record<string, unknown>}
 */
const isNode = (value) =>
  typeof value === 'object' && value !== null && typeof Reflect.get(value, 'type') === 'string';

/**
 * Recorre el árbol en profundidad.
 * @param {unknown} node
 * @param {(node: Record<string, unknown>) => void} visit
 */
function walk(node, visit) {
  if (Array.isArray(node)) {
    for (const item of node) walk(item, visit);
    return;
  }
  if (!isNode(node)) return;
  visit(node);
  for (const [key, value] of Object.entries(node)) {
    if (key !== 'type' && typeof value === 'object' && value !== null) walk(value, visit);
  }
}

/**
 * Texto constante: `"x"`, `'x'` o `` `x` `` sin sustituciones (Rolldown escribe el último).
 * @param {unknown} node
 * @returns {string | null}
 */
function stringLiteral(node) {
  if (!isNode(node)) return null;
  if (node.type === 'Literal') return typeof node.value === 'string' ? node.value : null;
  if (node.type !== 'TemplateLiteral' || !Array.isArray(node.quasis)) return null;
  if (!Array.isArray(node.expressions) || node.expressions.length > 0) return null;
  const [quasi] = node.quasis;
  const value = isNode(quasi) ? quasi.value : null;
  const cooked = typeof value === 'object' && value !== null ? Reflect.get(value, 'cooked') : null;
  return typeof cooked === 'string' ? cooked : null;
}

/**
 * @param {string} file ruta relativa a `dist/`
 * @returns {Imports}
 */
function importsOf(file) {
  const cached = graph.get(file);
  if (cached) return cached;
  /** @type {Imports} */
  const imports = { statics: [], dynamics: new Map() };
  graph.set(file, imports);
  if (!file.endsWith('.js')) return imports;
  const folder = file.includes('/') ? file.slice(0, file.lastIndexOf('/')) : '';
  const program = parseAst(readFileSync(join(dist, file), 'utf8'));

  // Lista de precarga de Vite: `const __vite__mapDeps = (i, m = …, d = (m.f || (m.f = [...])))`.
  /** @type {string[]} */
  let preloadList = [];
  walk(program, (node) => {
    if (
      node.type === 'VariableDeclarator' &&
      isNode(node.id) &&
      node.id.name === '__vite__mapDeps'
    ) {
      walk(node.init, (inner) => {
        if (inner.type !== 'ArrayExpression' || !Array.isArray(inner.elements)) return;
        const values = inner.elements.map(stringLiteral);
        if (values.length > 0 && values.every((value) => value !== null)) {
          preloadList = values.map((value) => mustExist(distPath(String(value), '')));
        }
      });
    }
  });

  walk(program, (node) => {
    const isStatic =
      node.type === 'ImportDeclaration' ||
      node.type === 'ExportAllDeclaration' ||
      node.type === 'ExportNamedDeclaration';
    if (isStatic) {
      const source = stringLiteral(node.source);
      if (source !== null) imports.statics.push(mustExist(distPath(source, folder)));
      return;
    }
    if (node.type !== 'CallExpression' || !Array.isArray(node.arguments)) return;
    // `precarga(() => import("./x.js"), __vite__mapDeps([0, 1, …]))`.
    const [loader, deps] = node.arguments;
    /** @type {string | null} */
    let target = null;
    walk(loader, (inner) => {
      if (inner.type === 'ImportExpression' && target === null)
        target = stringLiteral(inner.source);
    });
    if (target === null) return;
    /** @type {string[]} */
    const preloaded = [];
    if (
      isNode(deps) &&
      deps.type === 'CallExpression' &&
      isNode(deps.callee) &&
      deps.callee.name === '__vite__mapDeps' &&
      Array.isArray(deps.arguments)
    ) {
      walk(deps.arguments, (inner) => {
        if (inner.type !== 'ArrayExpression' || !Array.isArray(inner.elements)) return;
        for (const element of inner.elements) {
          const position = isNode(element) ? element.value : undefined;
          const entry = typeof position === 'number' ? preloadList[position] : undefined;
          if (entry === undefined)
            fail(`${file}: índice de precarga sin archivo (${String(position)}).`);
          preloaded.push(entry);
        }
      });
    }
    const resolved = mustExist(distPath(target, folder));
    imports.dynamics.set(resolved, [...(imports.dynamics.get(resolved) ?? []), ...preloaded]);
  });

  // Un `import()` sin envoltorio de precarga también es una porción.
  walk(program, (node) => {
    if (node.type !== 'ImportExpression') return;
    const source = stringLiteral(node.source);
    if (source === null) return;
    const resolved = mustExist(distPath(source, folder));
    if (!imports.dynamics.has(resolved)) imports.dynamics.set(resolved, []);
  });
  return imports;
}

/**
 * Cierre por importaciones estáticas.
 * @param {Iterable<string>} start
 */
function staticClosure(start) {
  const seen = new Set(start);
  const pending = [...seen];
  for (let file = pending.pop(); file !== undefined; file = pending.pop()) {
    for (const next of importsOf(file).statics) {
      if (!seen.has(next)) {
        seen.add(next);
        pending.push(next);
      }
    }
  }
  return seen;
}

// Paquete inicial: raíces, cierre estático y la precarga de un `import()` entre archivos iniciales.
let initial = staticClosure(initialRoots);
for (let grew = true; grew;) {
  grew = false;
  for (const file of [...initial]) {
    for (const [target, preloaded] of importsOf(file).dynamics) {
      if (!initial.has(target)) continue;
      const before = initial.size;
      initial = staticClosure([...initial, ...preloaded]);
      grew ||= initial.size > before;
    }
  }
}

// Porciones: cada destino de `import()` alcanzable que no es inicial.
/** @type {Map<string, Set<string>>} */
const chunks = new Map();
const visited = new Set();
const queue = [...initial];
for (let file = queue.shift(); file !== undefined; file = queue.shift()) {
  if (visited.has(file)) continue;
  visited.add(file);
  for (const [target, preloaded] of importsOf(file).dynamics) {
    if (!initial.has(target)) {
      const members = chunks.get(target) ?? new Set();
      for (const member of staticClosure([target, ...preloaded])) {
        if (!initial.has(member)) members.add(member);
      }
      chunks.set(target, members);
    }
    queue.push(target);
  }
  for (const next of importsOf(file).statics) queue.push(next);
}

// --- Medida -------------------------------------------------------------------------------------

/** @type {Map<string, { raw: number, gzip: number, brotli: number }>} */
const sizes = new Map();
/** @param {string} file */
function sizeOf(file) {
  const cached = sizes.get(file);
  if (cached) return cached;
  const content = readFileSync(join(dist, file));
  const measured = {
    raw: content.length,
    gzip: gzipSync(content, { level: 9 }).length,
    brotli: brotliCompressSync(content, {
      params: { [constants.BROTLI_PARAM_QUALITY]: constants.BROTLI_MAX_QUALITY },
    }).length,
  };
  sizes.set(file, measured);
  return measured;
}

/** @param {Iterable<string>} files */
function total(files) {
  const sum = { raw: 0, gzip: 0, brotli: 0, files: 0 };
  for (const file of files) {
    const size = sizeOf(file);
    sum.raw += size.raw;
    sum.gzip += size.gzip;
    sum.brotli += size.brotli;
    sum.files += 1;
  }
  return sum;
}

// Porción de pantalla: la que lleva la porción `scrNN` de `screenChunkName` (config/build.ts). Una
// pantalla puede cargarse por la porción de un módulo suyo (p. ej. `root_cause` arrastra `scr11`).
const SCREEN = /^(scr\d{2})-[\w-]+\.js$/;
/** @param {Iterable<string>} files */
const screensIn = (files) =>
  [...files]
    .map((file) => SCREEN.exec(basename(file))?.[1])
    .filter((screen) => screen !== undefined)
    .sort();
/** Nombre de la porción sin el hash de contenido. @param {string} file */
const chunkName = (file) => basename(file).replace(/-[\w-]{8}\.js$/, '');

const rows = [
  {
    name: 'paquete inicial',
    screens: screensIn(initial),
    limit: initialKb,
    ...total(initial),
  },
  ...[...chunks]
    .map(([target, members]) => ({
      name: chunkName(target),
      screens: screensIn(members),
      limit: chunkKb,
      ...total(members),
    }))
    .sort((a, b) => a.name.localeCompare(b.name)),
];
if (!rows.slice(1).some((row) => row.screens.length > 0)) {
  fail('no hay ninguna porción de pantalla (scrNN): el análisis de dist/ no encontró las cargas.');
}

const kb = (/** @type {number} */ bytes) => (bytes / 1000).toFixed(1);
const over = rows.filter((row) => row.gzip > row.limit * 1000);
const header = [
  '| Porción | Pantallas | Archivos | Sin comprimir (KB) | gzip (KB) | brotli (KB) | Límite gzip (KB) | Estado |',
  '|---|---|--:|--:|--:|--:|--:|---|',
];
const lines = rows.map(
  (row) =>
    `| ${row.name} | ${row.screens.join(', ') || '—'} | ${String(row.files)} | ${kb(row.raw)} | ` +
    `${kb(row.gzip)} | ${kb(row.brotli)} | ${String(row.limit)} | ` +
    `${row.gzip > row.limit * 1000 ? 'SUPERA' : 'cabe'} |`,
);
const verdict =
  over.length === 0
    ? `Presupuesto de tamaño (NFR-APP-01) cumplido: ${String(rows.length)} porciones.`
    : `Presupuesto de tamaño (NFR-APP-01) superado en ${String(over.length)}: ${over
        .map((row) => row.name)
        .join(', ')}.`;
const report = [
  '### Presupuesto de tamaño (NFR-APP-01)',
  '',
  `Inicial ≤ ${String(initialKb)} KB y cada porción ≤ ${String(chunkKb)} KB, gzip nivel 9 (1 KB = 1000 B).`,
  '',
  ...header,
  ...lines,
  '',
  verdict,
  '',
].join('\n');

process.stdout.write(report);
const summary = process.env.GITHUB_STEP_SUMMARY;
if (summary) appendFileSync(summary, `${report}\n`);
if (over.length > 0) {
  process.stderr.write(`${verdict}\n`);
  process.exit(1);
}
