#!/usr/bin/env node
/**
 * Comprobación de `npm run check` (VIG-290, BR-APP-02): toda clave de permiso de un descriptor de
 * pantalla está en la matriz exportada de `backend/openapi/u05-fixtures.json` (`permissions.keys`)
 * y no es de la plataforma (`platform.*`, sin pantalla); todo rol de `expected_roles` y
 * `home_for_roles` está en `permissions.roles`.
 *
 * Lee el texto de `src/pages/scrNN/descriptor.ts`: cada `required_permissions`, `expected_roles` y
 * `home_for_roles` debe ser una lista literal de textos (una variable o una expresión detienen la
 * comprobación, para que ninguna clave escape a la lectura). Con la carpeta por omisión exige las
 * dieciséis pantallas.
 *
 * Uso: `permissions.mjs [carpeta de páginas] [archivo de fixtures]` (las pruebas pasan otra carpeta).
 */
import { readdirSync, readFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../..', import.meta.url));
const defaultPages = join(root, 'src', 'pages');
const pages = resolve(process.argv[2] ?? defaultPages);
const fixtures = resolve(
  process.argv[3] ?? join(root, '..', 'backend', 'openapi', 'u05-fixtures.json'),
);

const { permissions } = JSON.parse(readFileSync(fixtures, 'utf8'));
const keys = new Set(permissions.keys);
const roles = new Set(permissions.roles);
const platformPrefix = permissions.platform_prefix;

/** @type {Record<string, (value: string) => string | null>} */
const FIELDS = {
  required_permissions: (value) => {
    if (!keys.has(value)) return `la clave «${value}» no está en la matriz exportada`;
    if (value.startsWith(platformPrefix)) return `la clave «${value}» es de la plataforma`;
    return null;
  },
  expected_roles: (value) => (roles.has(value) ? null : `el rol «${value}» no existe`),
  home_for_roles: (value) => (roles.has(value) ? null : `el rol «${value}» no existe`),
};

const LITERAL = /'([^'\\]*)'|"([^"\\]*)"/g;

/**
 * Quita comentarios de bloque y de línea (los descriptores no tienen `//` dentro de textos).
 * @param {string} source
 */
const withoutComments = (source) =>
  source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');

/**
 * @param {string} name
 * @param {string} source
 * @returns {{ errors: string[], count: number }}
 */
function checkDescriptor(name, source) {
  const errors = [];
  let count = 0;
  const code = withoutComments(source);
  for (const [field, validate] of Object.entries(FIELDS)) {
    const pattern = new RegExp(`\\b${field}\\b\\s*:?`, 'g');
    for (const match of code.matchAll(pattern)) {
      const rest = code.slice(match.index + match[0].length);
      const list = /^\s*\[([^\]]*)\]/.exec(rest);
      if (list === null || !match[0].endsWith(':')) {
        errors.push(`${name}: «${field}» debe ser una lista literal de textos.`);
        continue;
      }
      const body = list[1] ?? '';
      if (body.replace(LITERAL, '').replace(/[\s,]/g, '') !== '') {
        errors.push(`${name}: «${field}» solo admite textos literales.`);
        continue;
      }
      for (const literal of body.matchAll(LITERAL)) {
        count += 1;
        const problem = validate(literal[1] ?? literal[2] ?? '');
        if (problem !== null) errors.push(`${name}: ${problem}.`);
      }
    }
  }
  return { errors, count };
}

const screens = readdirSync(pages, { withFileTypes: true })
  .filter((entry) => entry.isDirectory() && /^scr\d{2}$/.test(entry.name))
  .map((entry) => entry.name)
  .sort();

const errors = [];
let total = 0;
if (pages === defaultPages) {
  const expected = Array.from({ length: 16 }, (_, i) => `scr${String(i + 1).padStart(2, '0')}`);
  for (const screen of expected) {
    if (!screens.includes(screen)) errors.push(`Falta la pantalla src/pages/${screen}.`);
  }
}
for (const screen of screens) {
  const name = `${screen}/descriptor.ts`;
  let source;
  try {
    source = readFileSync(join(pages, screen, 'descriptor.ts'), 'utf8');
  } catch {
    errors.push(`${name}: no existe.`);
    continue;
  }
  const result = checkDescriptor(name, source);
  errors.push(...result.errors);
  total += result.count;
}

if (screens.length === 0) errors.push(`No hay pantallas en ${pages}.`);

if (errors.length > 0) {
  for (const error of errors) process.stderr.write(`${error}\n`);
  process.stderr.write(
    `Claves o roles fuera de la matriz de u05-fixtures.json (BR-APP-02): ${String(errors.length)}.\n`,
  );
  process.exit(1);
}
process.stdout.write(
  `Descriptores de pantalla: ${String(screens.length)}; claves y roles comprobados: ${String(total)}.\n`,
);
