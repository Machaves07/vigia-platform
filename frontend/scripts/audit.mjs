#!/usr/bin/env node
/**
 * `npm run audit:check` (NFR-APP-25; VIG-294): `npm audit` con alta o crítica bloqueante sobre **todo**
 * el árbol (producción y desarrollo), con una única salvedad registrada abajo.
 *
 * Salvedad (aviso de VIG-277 en VIG-294, decisión del dueño pendiente): `@lhci/cli` 0.15.1, la
 * última publicada, arrastra avisos altos (`lighthouse` → `puppeteer-core`, `proxy-agent`,
 * `tmp`, `uuid`…) sin arreglo; `npm audit fix --force` propone bajar a 0.1.0. Es de desarrollo,
 * solo la usa `nightly` (VIG-330) y no llega a `dist/` ni a la imagen. Un aviso se exceptúa solo
 * si **todas** sus rutas hacia `package.json` pasan por esa dependencia directa; si el mismo
 * paquete vulnerable llega también por otra, o si el aviso alcanza producción, bloquea. La
 * excepción vence en su fecha de revisión.
 *
 * Las rutas salen de `package-lock.json`, no del informe: desde cada instalación vulnerable
 * (`nodes` del informe; si falta, toda instalación con ese nombre) se sube por quién la requiere,
 * con la resolución de `node_modules` de Node, hasta las dependencias directas de
 * `package.json`. El campo `effects` del informe no basta: npm 10 lo deja vacío en algún paquete
 * de un ciclo (`@lhci/utils`), y npm 11 no.
 *
 * Uso: `audit.mjs [--input informe.json] [--lock package-lock.json] [--today AAAA-MM-DD]` (las
 * pruebas pasan un informe y un bloqueo sintéticos). Termina en 0 si nada alto o crítico queda
 * sin excepción, en 1 si queda y en 2 si no puede auditar.
 */
import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { systemClock } from '../src/state/clock.ts';

const root = fileURLToPath(new URL('..', import.meta.url));
const BLOCKING = new Set(['high', 'critical']);

/** @type {ReadonlyArray<{ root: string, review: string, reason: string }>} */
const EXCEPTIONS = [
  {
    root: '@lhci/cli',
    review: '2026-12-31',
    reason:
      'Lighthouse CI de nightly (VIG-330), solo desarrollo; sin versión que corrija los avisos.',
  },
];

/**
 * @param {string} message
 * @returns {never}
 */
function stop(message) {
  process.stderr.write(`audit.mjs: ${message}\n`);
  process.exit(2);
}

/** @type {string | null} */
let input = null;
let lockPath = join(root, 'package-lock.json');
let today = new Date(systemClock.now()).toISOString().slice(0, 10);
const argv = process.argv.slice(2);
for (let index = 0; index < argv.length; index += 1) {
  const argument = argv[index];
  const value = argv[index + 1];
  if (argument === '--input' && value !== undefined) input = value;
  else if (argument === '--lock' && value !== undefined) lockPath = value;
  else if (argument === '--today' && value !== undefined && /^\d{4}-\d{2}-\d{2}$/.test(value)) {
    today = value;
  } else stop(`argumento no válido: ${String(argument)}`);
  index += 1;
}

/** @returns {string} */
function readReport() {
  if (input !== null) return readFileSync(input, 'utf8');
  const result = spawnSync('npm', ['audit', '--json'], { cwd: root, encoding: 'utf8' });
  if (result.error) stop(`no se pudo ejecutar npm audit: ${String(result.error)}`);
  // npm audit termina en 1 cuando hay avisos: lo que cuenta es el informe.
  return result.stdout;
}

/** @type {{ vulnerabilities?: Record<string, { severity: string, nodes?: string[], via: unknown[] }>, error?: unknown }} */
let report;
try {
  report = JSON.parse(readReport());
} catch (error) {
  stop(`el informe de npm audit no es JSON: ${String(error)}`);
}
if (report.error !== undefined || typeof report.vulnerabilities !== 'object') {
  stop(`npm audit no devolvió un informe: ${JSON.stringify(report.error ?? report)}`);
}
const vulnerabilities = report.vulnerabilities;

/** @type {Record<string, Record<string, unknown>>} */
let packages;
try {
  packages = JSON.parse(readFileSync(lockPath, 'utf8')).packages;
} catch (error) {
  stop(`no se pudo leer ${lockPath}: ${String(error)}`);
}
if (typeof packages !== 'object' || packages === null || typeof packages[''] !== 'object') {
  stop(`${lockPath} no tiene «packages» con la raíz (se espera lockfileVersion 3).`);
}

/**
 * Nombres de las dependencias declaradas de una entrada del bloqueo.
 * @param {Record<string, unknown>} entry
 */
const declared = (entry) =>
  ['dependencies', 'devDependencies', 'optionalDependencies', 'peerDependencies'].flatMap(
    (field) => {
      const value = entry[field];
      return typeof value === 'object' && value !== null ? Object.keys(value) : [];
    },
  );

/**
 * Instalación de `name` que ve `from` (resolución de `node_modules`), o `null`.
 * @param {string} from
 * @param {string} name
 */
function resolveFrom(from, name) {
  for (let base = from; ;) {
    const candidate = base === '' ? `node_modules/${name}` : `${base}/node_modules/${name}`;
    if (packages[candidate] !== undefined) return candidate;
    if (base === '') return null;
    const cut = base.lastIndexOf('/node_modules/');
    base = cut === -1 ? '' : base.slice(0, cut);
  }
}

/** Quién requiere cada instalación (aristas invertidas del bloqueo). */
/** @type {Map<string, Set<string>>} */
const requiredBy = new Map();
for (const [path, entry] of Object.entries(packages)) {
  for (const name of declared(entry)) {
    // Las de desarrollo solo cuentan desde la raíz: npm no instala las de un paquete publicado.
    const fromRootOnly = typeof entry.devDependencies === 'object' && path !== '';
    if (fromRootOnly && Object.hasOwn(Object(entry.devDependencies), name)) continue;
    const target = resolveFrom(path, name);
    if (target === null) continue;
    const parents = requiredBy.get(target) ?? new Set();
    parents.add(path);
    requiredBy.set(target, parents);
  }
}

/** @param {string} path */
const nameOf = (path) => path.slice(path.lastIndexOf('node_modules/') + 'node_modules/'.length);

/**
 * Dependencias directas de `package.json` desde las que se llega a alguna instalación de `name`.
 * @param {string} name
 * @param {string[] | undefined} nodes
 */
function directRoots(name, nodes) {
  const starts =
    nodes && nodes.length > 0
      ? nodes
      : Object.keys(packages).filter((path) => path !== '' && nameOf(path) === name);
  const roots = new Set();
  if (starts.length === 0) roots.add(`${name} (sin instalación en el bloqueo)`);
  const seen = new Set();
  const pending = [...starts];
  for (let current = pending.pop(); current !== undefined; current = pending.pop()) {
    if (seen.has(current)) continue;
    seen.add(current);
    const parents = requiredBy.get(current);
    if (parents === undefined || parents.size === 0) {
      // Nadie la requiere según el bloqueo: no se puede demostrar la ruta; bloquea.
      roots.add(`${nameOf(current)} (ruta desconocida)`);
      continue;
    }
    for (const parent of parents) {
      if (parent === '') roots.add(nameOf(current));
      else pending.push(parent);
    }
  }
  return roots;
}

/** @param {unknown[]} via */
const titles = (via) =>
  via
    .map((item) => (typeof item === 'object' && item !== null ? Reflect.get(item, 'title') : null))
    .filter((title) => typeof title === 'string');

const blocking = [];
const excepted = [];
const used = new Set();
for (const [name, entry] of Object.entries(vulnerabilities)) {
  if (!BLOCKING.has(entry.severity)) continue;
  const roots = [...directRoots(name, entry.nodes)].sort();
  const exceptions = roots.map((item) => EXCEPTIONS.find((exception) => exception.root === item));
  const line = `${name} (${entry.severity}) por ${roots.join(', ') || '¿?'}${
    titles(entry.via).length > 0 ? `: ${titles(entry.via).join('; ')}` : ''
  }`;
  if (roots.length > 0 && exceptions.every((exception) => exception !== undefined)) {
    for (const exception of exceptions) used.add(exception);
    excepted.push(line);
  } else {
    blocking.push(line);
  }
}

for (const exception of used) {
  if (exception.review < today) {
    blocking.push(`excepción de ${exception.root} vencida el ${exception.review}: revisarla`);
  }
}

if (excepted.length > 0) {
  process.stdout.write(`Avisos altos o críticos exceptuados (${String(excepted.length)}):\n`);
  for (const line of excepted) process.stdout.write(`- ${line}\n`);
}
if (blocking.length > 0) {
  process.stderr.write(
    `npm audit: avisos altos o críticos sin excepción (${String(blocking.length)}):\n`,
  );
  for (const line of blocking) process.stderr.write(`- ${line}\n`);
  process.exit(1);
}
process.stdout.write('npm audit: ningún aviso alto o crítico fuera de las excepciones.\n');
