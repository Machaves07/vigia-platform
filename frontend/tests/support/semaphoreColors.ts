/**
 * Color de refuerzo del semáforo leído de las variables CSS (PR-APP-08): la regla
 * `.chip[data-tone='…']` de `ObservabilityBadge.module.css` y, si usa `var(--…)`, su valor en
 * `tokens.css`. La clasificación por tono y saturación decide azul, ámbar, gris, verde o rojo.
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const read = (path: string) => readFileSync(fileURLToPath(new URL(path, import.meta.url)), 'utf8');

const tokensCss = read('../../src/styles/tokens.css');
const badgeCss = read('../../src/components/ObservabilityBadge/ObservabilityBadge.module.css');

export type ColorFamily = 'blue' | 'amber' | 'grey' | 'green' | 'red' | 'other';

function declarations(css: string): Map<string, string> {
  const result = new Map<string, string>();
  for (const match of css.matchAll(/(--[a-z0-9-]+):\s*([^;]+);/g)) {
    const [, name = '', value = ''] = match;
    result.set(name, value.trim());
  }
  return result;
}

const tokens = declarations(tokensCss);

function resolve(value: string): string {
  const reference = /^var\((--[a-z0-9-]+)\)$/.exec(value);
  if (reference === null) return value;
  const resolved = tokens.get(reference[1] ?? '');
  if (resolved === undefined) throw new Error(`Variable sin definir en tokens.css: ${value}`);
  return resolve(resolved);
}

/** Fondo `#rrggbb` de la regla del tono, resuelto contra `tokens.css`. */
export function toneBackground(tone: string): string {
  const rule = new RegExp(`\\.chip\\[data-tone='${tone}'\\]\\s*\\{([^}]*)\\}`).exec(badgeCss);
  if (rule === null) throw new Error(`Sin regla CSS para el tono «${tone}».`);
  const background = /background-color:\s*([^;]+);/.exec(rule[1] ?? '');
  if (background === null) throw new Error(`El tono «${tone}» no declara background-color.`);
  const color = resolve((background[1] ?? '').trim()).toLowerCase();
  if (!/^#[0-9a-f]{6}$/.test(color)) throw new Error(`Color no #rrggbb: ${color}`);
  return color;
}

/** Todos los valores de color del módulo del semáforo, resueltos. */
export function allBadgeColors(): string[] {
  const colors: string[] = [];
  for (const match of badgeCss.matchAll(/(?:^|[\s;{])(?:background-)?color:\s*([^;]+);/g)) {
    colors.push(resolve((match[1] ?? '').trim()).toLowerCase());
  }
  return colors;
}

export function colorFamily(color: string): ColorFamily {
  const [r = 0, g = 0, b = 0] = [1, 3, 5].map((i) => parseInt(color.slice(i, i + 2), 16) / 255);
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const delta = max - min;
  const lightness = (max + min) / 2;
  const saturation = delta === 0 ? 0 : delta / (1 - Math.abs(2 * lightness - 1));
  if (saturation < 0.2) return 'grey';
  let hue: number;
  if (max === r) hue = 60 * (((g - b) / delta) % 6);
  else if (max === g) hue = 60 * ((b - r) / delta + 2);
  else hue = 60 * ((r - g) / delta + 4);
  hue = (hue + 360) % 360;
  if (hue < 20 || hue >= 330) return 'red';
  if (hue < 55) return 'amber';
  if (hue >= 75 && hue < 170) return 'green';
  if (hue >= 190 && hue < 250) return 'blue';
  return 'other';
}
