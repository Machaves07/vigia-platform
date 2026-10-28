// @vitest-environment node
/**
 * Variables del sistema de diseño (LC-APP-23): cada pareja texto y fondo cumple 4,5:1 y el
 * semáforo es azul, ámbar y gris, sin verde ni rojo semánticos (P2).
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

const tokensCss = readFileSync(
  fileURLToPath(new URL('../../../src/styles/tokens.css', import.meta.url)),
  'utf8',
);

function tokens(css: string): Map<string, string> {
  const result = new Map<string, string>();
  for (const match of css.matchAll(/(--[a-z0-9-]+):\s*([^;]+);/g)) {
    const [, name = '', value = ''] = match;
    result.set(name, value.trim());
  }
  return result;
}

function hex(name: string): string {
  const value = tokens(tokensCss).get(name);
  if (value === undefined || !/^#[0-9a-f]{6}$/.test(value)) {
    throw new Error(`${name} no es un color #rrggbb: ${String(value)}`);
  }
  return value;
}

function luminance(color: string): number {
  const [r = 0, g = 0, b = 0] = [1, 3, 5].map((i) => {
    const channel = parseInt(color.slice(i, i + 2), 16) / 255;
    return channel <= 0.03928 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a: string, b: string): number {
  const [light, dark] = [luminance(a), luminance(b)].sort((x, y) => y - x) as [number, number];
  return (light + 0.05) / (dark + 0.05);
}

function hue(color: string): { hue: number; saturation: number } {
  const [r = 0, g = 0, b = 0] = [1, 3, 5].map((i) => parseInt(color.slice(i, i + 2), 16) / 255);
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const delta = max - min;
  const lightness = (max + min) / 2;
  const saturation = delta === 0 ? 0 : delta / (1 - Math.abs(2 * lightness - 1));
  let h = 0;
  if (delta !== 0) {
    if (max === r) h = 60 * (((g - b) / delta) % 6);
    else if (max === g) h = 60 * ((b - r) / delta + 2);
    else h = 60 * ((r - g) / delta + 4);
  }
  return { hue: (h + 360) % 360, saturation };
}

const PAIRS: readonly (readonly [string, string])[] = [
  ['--color-text', '--color-surface'],
  ['--color-text', '--color-surface-muted'],
  ['--color-text-muted', '--color-surface'],
  ['--color-text-muted', '--color-surface-muted'],
  ['--color-on-observable', '--color-observable'],
  ['--color-on-degraded', '--color-degraded'],
  ['--color-on-unobservable', '--color-unobservable'],
  ['--focus-ring-color', '--color-surface'],
  ['--focus-ring-color', '--color-surface-muted'],
  // Sistema visual v1 (VIG-364).
  ['--color-text', '--color-bg'],
  ['--color-text-muted', '--color-bg'],
  ['--color-on-action', '--color-action'],
  ['--color-on-action', '--color-action-hover'],
  ['--color-action', '--color-action-subtle'],
  ['--color-action', '--color-surface'],
  ['--color-text', '--color-next-step'],
  ['--focus-ring-color', '--color-bg'],
  ['--state-overdue-text', '--state-overdue-bg'],
  ['--state-due-soon-text', '--state-due-soon-bg'],
  ['--state-open-text', '--state-open-bg'],
  ['--state-closed-text', '--state-closed-bg'],
  ['--state-unclosable-text', '--state-unclosable-bg'],
  ['--state-neutral-text', '--state-neutral-bg'],
];

/** Estados del lazo: hablan del plazo de una acción, nunca de una zona (ver `tokens.css`). */
const LOOP_STATE = '--state-';

describe('tokens de estilo', () => {
  it.each(PAIRS)('%s sobre %s cumple 4,5:1', (foreground, background) => {
    expect(contrast(hex(foreground), hex(background))).toBeGreaterThanOrEqual(4.5);
  });

  it('el semáforo es azul, ámbar y gris: ningún color con tono verde o rojo saturado', () => {
    expect(hue(hex('--color-observable')).hue).toBeGreaterThanOrEqual(190);
    expect(hue(hex('--color-observable')).hue).toBeLessThanOrEqual(250);
    expect(hue(hex('--color-degraded')).hue).toBeGreaterThanOrEqual(30);
    expect(hue(hex('--color-degraded')).hue).toBeLessThanOrEqual(50);
    expect(hue(hex('--color-unobservable')).saturation).toBeLessThan(0.15);
    for (const [name, value] of tokens(tokensCss)) {
      if (!/^#[0-9a-f]{6}$/.test(value) || name.startsWith(LOOP_STATE)) continue;
      const { hue: h, saturation } = hue(value);
      if (saturation < 0.15) continue;
      const greenOrRed = (h >= 75 && h <= 165) || h <= 15 || h >= 345;
      expect(greenOrRed, `${name} (${value}) tiene tono verde o rojo`).toBe(false);
    }
  });

  it('el objetivo táctil es de 44 px (48 en tableta) y la tipografía es IBM Plex propia', () => {
    expect(tokens(tokensCss).get('--touch-target-min')).toBe('44px');
    expect(tokens(tokensCss).get('--touch-target-tablet')).toBe('48px');
    expect(tokens(tokensCss).get('--font-family')).toMatch(/^'IBM Plex Sans', system-ui,/);
    expect(tokens(tokensCss).get('--font-family-mono')).toMatch(/^'IBM Plex Mono', ui-monospace,/);
  });

  it('las fuentes se sirven desde la aplicación, nunca desde otro origen (CSP self)', () => {
    const sources = [...tokensCss.matchAll(/url\(([^)]*)\)/g)].map((match) => match[1] ?? '');
    expect(sources.length).toBe(5);
    for (const source of sources) expect(source).toMatch(/^'\.\/fonts\/[a-z0-9-]+\.woff2'$/);
    expect(tokensCss).not.toMatch(/@import|https?:/i);
  });

  it('la observabilidad no usa los colores de los estados del lazo', () => {
    const badge = readFileSync(
      fileURLToPath(
        new URL(
          '../../../src/components/ObservabilityBadge/ObservabilityBadge.module.css',
          import.meta.url,
        ),
      ),
      'utf8',
    );
    expect(badge).toMatch(/var\(--color-(?:observable|degraded|unobservable)\)/);
    expect(badge).not.toMatch(/--state-/);
  });
});
