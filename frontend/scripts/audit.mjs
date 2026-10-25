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
 * Las rutas salen de `effects` del informe de `npm audit --json`: npm marca vulnerable a todo
 * paquete que depende de uno vulnerable, hasta la dependencia directa (`isDirect`).
 *
 * Uso: `audit.mjs [--input informe.json] [--today AAAA-MM-DD]` (las pruebas pasan un informe
 * sintético). Termina en 0 si nada alto o crítico queda sin excepción, en 1 si queda y en 2 si
 * no puede auditar.
 */
import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
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
let today = new Date(systemClock.now()).toISOString().slice(0, 10);
const argv = process.argv.slice(2);
for (let index = 0; index < argv.length; index += 1) {
  const argument = argv[index];
  const value = argv[index + 1];
  if (argument === '--input' && value !== undefined) input = value;
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

/** @type {{ vulnerabilities?: Record<string, { severity: string, isDirect: boolean, effects: string[], via: unknown[] }>, error?: unknown }} */
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

/**
 * Dependencias directas desde las que se llega a `name` (subiendo por `effects`).
 * @param {string} name
 */
function directRoots(name) {
  const roots = new Set();
  const seen = new Set();
  const pending = [name];
  for (let current = pending.pop(); current !== undefined; current = pending.pop()) {
    if (seen.has(current)) continue;
    seen.add(current);
    const entry = vulnerabilities[current];
    if (entry === undefined) {
      // Un dependiente que el informe no lista: no se puede demostrar la ruta; bloquea.
      roots.add(`${current} (ruta desconocida)`);
      continue;
    }
    if (entry.isDirect) roots.add(current);
    pending.push(...entry.effects);
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
  const roots = [...directRoots(name)].sort();
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
