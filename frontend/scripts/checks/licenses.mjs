#!/usr/bin/env node
/**
 * Comprobación de `npm run check` (NFR-APP-25; VIG-294): la licencia de **cada** paquete del árbol
 * de `package-lock.json` (producción y desarrollo, directos y transitivos) está en la lista
 * permitida de AGENTS.md (MIT, BSD-2, BSD-3, Apache-2.0, ISC, PSF y MPL-2.0) o en una excepción de
 * la adenda A-43. `tests/unit/build/dependencies.test.ts` ya revisa las dependencias directas;
 * esta comprobación cubre las transitivas, que no tienen fila en `DEPENDENCIES.md`.
 *
 * - La licencia sale del campo `license` del bloqueo y, si falta, del `package.json` instalado
 *   (`license` o el antiguo `licenses: [{type}]`, que exige **todas** sus entradas). Sin licencia
 *   declarada, falla.
 * - Expresiones SPDX: `A OR B` basta con que una alternativa valga; `A AND B` exige todas;
 *   `A WITH excepción` se juzga por `A`. Cualquier otra forma (`SEE LICENSE IN`, `UNLICENSED`,
 *   texto libre) falla, salvo en los paquetes propios (`@vigia/contracts`).
 * - Excepciones (A-43): con alcance `cualquiera` valen para todo paquete; con alcance
 *   `desarrollo` solo para paquetes que el bloqueo marca `dev` (nunca llegan a `dist/` ni a la
 *   imagen), el mismo criterio que `chardet` y EX-06/EX-07. Una excepción cuya fecha de revisión
 *   pasó también falla.
 *
 * Uso: `licenses.mjs [package-lock.json] [--today AAAA-MM-DD]` (las pruebas pasan un bloqueo
 * sintético y una fecha). Termina en 0 si todo cumple, en 1 si no y en 2 si no puede leer.
 */
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { systemClock } from '../../src/state/clock.ts';

const root = fileURLToPath(new URL('../..', import.meta.url));

/** Lista permitida de AGENTS.md (convenciones) y NFR-CTR-26. */
const ALLOWED = new Set([
  'MIT',
  'BSD-2-Clause',
  'BSD-3-Clause',
  'BSD', // `licenses: [{type: "BSD"}]` antiguo, sin variante; igual que tools/check_licenses.py.
  'Apache-2.0',
  'ISC',
  'PSF-2.0',
  'Python-2.0', // Identificador SPDX de la licencia de la PSF (p. ej. `argparse`).
  'MPL-2.0',
]);

/**
 * Excepciones de la adenda A-43 (2026-09-28), con su alcance y fecha de revisión.
 * @type {ReadonlyArray<{ id: string, license: string, scope: 'cualquiera' | 'desarrollo', review: string, reason: string }>}
 */
const EXCEPTIONS = [
  {
    id: 'A-43.1',
    license: 'MIT-0',
    scope: 'cualquiera',
    review: '2027-03-31',
    reason: 'MIT sin cláusula de atribución (EX-02 de vigia-contracts).',
  },
  {
    id: 'A-43.1',
    license: 'BlueOak-1.0.0',
    scope: 'cualquiera',
    review: '2027-03-31',
    reason: 'Permisiva, igual o más que MIT (EX-03 de vigia-contracts, minimatch).',
  },
  {
    id: 'A-43.1',
    license: '0BSD',
    scope: 'cualquiera',
    review: '2027-03-31',
    reason: 'BSD sin condiciones (EX-04 de vigia-contracts).',
  },
  {
    id: 'A-43.1',
    license: 'Zlib',
    scope: 'cualquiera',
    review: '2027-03-31',
    reason: 'Permisiva (EX-04 de vigia-contracts).',
  },
  {
    id: 'A-43.1',
    license: 'CC0-1.0',
    scope: 'cualquiera',
    review: '2027-03-31',
    reason: 'Dominio público (EX-04 de vigia-contracts).',
  },
  {
    id: 'A-43, EX-06/07',
    license: 'CC-BY-3.0',
    scope: 'desarrollo',
    review: '2027-03-31',
    reason: 'Solo herramientas de desarrollo que no se distribuyen (spdx-exceptions).',
  },
  {
    id: 'A-71 (extiende A-43 al frontend)',
    license: 'CC-BY-4.0',
    scope: 'desarrollo',
    review: '2027-03-31',
    reason:
      'Datos de caniuse-lite (browserslist), solo en construcción; mismo criterio que CC-BY-3.0.',
  },
];

/** Paquetes del proyecto: su licencia es la del dueño. */
const OWN = new Set(['@vigia/contracts']);

let lockPath = join(root, 'package-lock.json');
let today = new Date(systemClock.now()).toISOString().slice(0, 10);
const argv = process.argv.slice(2);
for (let index = 0; index < argv.length; index += 1) {
  const argument = argv[index] ?? '';
  if (argument === '--today') {
    today = argv[index + 1] ?? '';
    index += 1;
    if (!/^\d{4}-\d{2}-\d{2}$/.test(today)) stop('--today necesita una fecha AAAA-MM-DD.');
  } else {
    lockPath = resolve(argument);
  }
}

/**
 * @param {string} message
 * @returns {never}
 */
function stop(message) {
  process.stderr.write(`licenses.mjs: ${message}\n`);
  process.exit(2);
}

/** @type {Record<string, unknown>} */
let lock;
try {
  lock = JSON.parse(readFileSync(lockPath, 'utf8'));
} catch (error) {
  stop(`no se pudo leer ${lockPath}: ${String(error)}`);
}
const packages = lock.packages;
if (typeof packages !== 'object' || packages === null || Array.isArray(packages)) {
  stop(`${lockPath} no tiene «packages» (se espera lockfileVersion 3).`);
}

/**
 * Hojas de una expresión SPDX que la hacen válida, o `null` si no puede valer.
 * @param {string} expression
 * @param {(leaf: string) => boolean} accepted
 * @returns {boolean | null} `null` si la expresión no se entiende
 */
function evaluate(expression, accepted) {
  const tokens = expression.match(/\(|\)|[^\s()]+/g) ?? [];
  let position = 0;
  /** @returns {boolean | null} */
  const primary = () => {
    const token = tokens[position];
    if (token === undefined) return null;
    position += 1;
    if (token === '(') {
      const inner = disjunction();
      if (tokens[position] !== ')') return null;
      position += 1;
      return inner;
    }
    if (/^(?:AND|OR|WITH|\))$/.test(token) || !/^[A-Za-z0-9.+-]+$/.test(token)) return null;
    const value = accepted(token.replace(/\+$/, ''));
    if (tokens[position] === 'WITH') {
      position += 1;
      const exception = tokens[position];
      if (exception === undefined || !/^[A-Za-z0-9.-]+$/.test(exception)) return null;
      position += 1;
    }
    return value;
  };
  /** @returns {boolean | null} */
  const conjunction = () => {
    let value = primary();
    while (value !== null && tokens[position] === 'AND') {
      position += 1;
      const next = primary();
      value = next === null ? null : value && next;
    }
    return value;
  };
  /** @returns {boolean | null} */
  const disjunction = () => {
    let value = conjunction();
    while (value !== null && tokens[position] === 'OR') {
      position += 1;
      const next = conjunction();
      value = next === null ? null : value || next;
    }
    return value;
  };
  const result = disjunction();
  return position === tokens.length ? result : null;
}

/**
 * Licencia declarada de una entrada: la del bloqueo o la del `package.json` instalado.
 * @param {string} path
 * @param {Record<string, unknown>} entry
 * @returns {string | null}
 */
function declaredLicense(path, entry) {
  if (typeof entry.license === 'string') return entry.license;
  const installed = join(dirname(lockPath), path, 'package.json');
  if (!existsSync(installed)) return null;
  const manifest = JSON.parse(readFileSync(installed, 'utf8'));
  if (typeof manifest.license === 'string') return manifest.license;
  if (typeof manifest.license === 'object' && typeof manifest.license?.type === 'string') {
    return manifest.license.type;
  }
  if (Array.isArray(manifest.licenses) && manifest.licenses.length > 0) {
    const types = manifest.licenses.map((/** @type {unknown} */ item) =>
      typeof item === 'string' ? item : Reflect.get(Object(item), 'type'),
    );
    if (types.every((/** @type {unknown} */ type) => typeof type === 'string')) {
      return types.length === 1 ? types[0] : `(${types.join(' AND ')})`;
    }
  }
  return null;
}

const problems = [];
const used = new Set();
let checked = 0;
for (const [path, raw] of Object.entries(packages)) {
  if (path === '' || typeof raw !== 'object' || raw === null) continue;
  /** @type {Record<string, unknown>} */
  const entry = raw;
  if (entry.link === true) continue;
  const name =
    typeof entry.name === 'string'
      ? entry.name
      : path.slice(path.lastIndexOf('node_modules/') + 13);
  const version = typeof entry.version === 'string' ? entry.version : '?';
  checked += 1;
  if (OWN.has(name)) continue;
  const license = declaredLicense(path, entry);
  if (license === null) {
    problems.push(`${name}@${version}: sin licencia declarada (${path})`);
    continue;
  }
  const development = entry.dev === true;
  const verdict = evaluate(license, (leaf) => {
    if (ALLOWED.has(leaf)) return true;
    const exception = EXCEPTIONS.find((item) => item.license === leaf);
    if (exception === undefined) return false;
    if (exception.scope === 'desarrollo' && !development) return false;
    used.add(exception);
    return true;
  });
  if (verdict === null) {
    problems.push(`${name}@${version}: licencia «${license}» no es una expresión SPDX reconocida`);
  } else if (!verdict) {
    const scope = development ? 'desarrollo' : 'producción';
    problems.push(`${name}@${version}: licencia «${license}» no permitida (${scope}; ${path})`);
  }
}

for (const exception of used) {
  if (exception.review < today) {
    problems.push(
      `excepción ${exception.id} (${exception.license}) vencida el ${exception.review}: revisarla`,
    );
  }
}

if (problems.length > 0) {
  process.stderr.write(`Licencias no permitidas (${String(problems.length)}):\n`);
  for (const problem of problems) process.stderr.write(`- ${problem}\n`);
  process.exit(1);
}
process.stdout.write(
  `Licencias permitidas: ${String(checked)} paquetes; excepciones de A-43 en uso: ${
    [...used].map((item) => item.license).join(', ') || 'ninguna'
  }.\n`,
);
