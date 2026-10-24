import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

import { checkTexts, textViolations } from '../../scripts/lib/texts_catalog.mjs';
import { text } from '@/texts';

const root = process.cwd();

describe('catálogo fijo (BR-APP-26/27/29/38/41/83)', () => {
  it('todos los dominios pasan la lista propia', () => {
    expect(checkTexts(join(root, 'src/texts'))).toEqual([]);
  });

  it.each([
    'seguro',
    'zona segura',
    'zonas seguras',
    'despejado',
    'despejada',
    'vacío',
    'vacías',
    'sabotaje',
    'manipulación deliberada',
    'garantiza',
    'garantizamos',
    'garantía',
    'reduce accidentes',
    'eficiencia',
    'productividad',
    'desempeño',
    '95 % de detección',
    '95% detección',
    'detección del 99 %',
    'promete resultados',
    'compromiso de calidad',
    'aseguramos resultados',
    '0 hallazgos',
    'no ocurrió',
    'ＺＯＮＡ ＳＥＧＵＲＡ',
    'zona\u00a0segura',
    'se\u200bguro',
    'Observable no significa zona despejada; zona segura',
  ])('rechaza %s', (value) => {
    expect(textViolations('probe.caption', value).length).toBeGreaterThan(0);
  });

  it.each([
    'Observable no significa zona despejada',
    'Observable no equivale a zona despejada',
    'Observable no significa zona segura',
    'Observable no equivale a zona segura',
    'OBSERVABLE NO SIGNIFICA ZONA DESPEJADA',
    'No se observó',
    '95 % de cobertura observable medida',
    'Seguridad industrial',
    'Sin acuerdo de uso',
    'Obstrucción',
    'Compromiso registrado sobre la zona',
  ])('permite %s sin ampliar D-9', (value) => {
    expect(textViolations('probe.caption', value)).toEqual([]);
  });

  it('D-9 no es permiso para otras formas ni para el resto del texto', () => {
    expect(textViolations('probe.caption', 'no significa zona despejadas')).toEqual([
      'probe.caption: término bloqueado «despejadas»',
    ]);
    expect(textViolations('probe.caption', 'no implica zona segura')).toEqual([
      'probe.caption: término bloqueado «segura»',
    ]);
  });

  it('toda clave goal* exige la marca; no basta en otra entrada ni en una subpalabra', () => {
    for (const key of ['metrics.goal', 'metrics.goals.activation', 'metrics.goal_quality']) {
      expect(textViolations(key, 'Meta del periodo')).toEqual([
        `${key}: falta la marca «objetivo propio»`,
      ]);
      expect(textViolations(key, 'Meta del periodo [objetivo propio]')).toEqual([]);
      expect(textViolations(key, 'objetivo propios')).toEqual([
        `${key}: falta la marca «objetivo propio»`,
      ]);
      expect(textViolations(key, 'Compromiso del periodo [objetivo propio]')).toEqual([
        `${key}: una meta no se presenta como compromiso`,
      ]);
    }
  });

  it('una confianza nunca es certeza, tanto en clave como en texto', () => {
    expect(textViolations('metrics.max_confidence', 'Certeza')).toEqual([
      'metrics.max_confidence: confianza no se presenta como certeza',
    ]);
    expect(textViolations('probe.caption', 'Confianza con certeza')).toEqual([
      'probe.caption: confianza no se presenta como certeza',
    ]);
    expect(textViolations('metrics.max_confidence', 'Confianza máxima')).toEqual([]);
  });

  it.each(['intencional', 'deliberada', 'adrede', 'a propósito', 'culpable', 'negligencia'])(
    'degradación neutral, no %s',
    (value) => {
      expect(textViolations('degradation.caption', value)).toEqual([
        'degradation.caption: etiqueta de degradación con intención',
      ]);
    },
  );

  it.each([
    ['zona segura', 'segura'],
    ['garantiza', 'garantiza'],
  ])('mutación CLI %s nombra clave y término', (value, term) => {
    const cache = join(root, 'node_modules/.cache/vig287');
    mkdirSync(cache, { recursive: true });
    const directory = mkdtempSync(join(cache, 'texts-'));
    try {
      writeFileSync(join(directory, 'scr99.json'), JSON.stringify({ 'scr99.probe': value }));
      const result = spawnSync(
        process.execPath,
        [join(root, 'scripts/checks/texts.mjs'), directory],
        { encoding: 'utf8', timeout: 60_000 },
      );
      expect(result.status).toBe(1);
      expect(result.stderr.trim()).toBe(`scr99.json: scr99.probe: término bloqueado «${term}»`);
    } finally {
      rmSync(directory, { recursive: true, force: true });
    }
  });

  it('textos transversales consumibles por cualquier pantalla', () => {
    expect(text('common.not_available_in_scope')).toBe('No disponible en su alcance');
    expect(text('common.updated_ago')).toBe('Actualizado hace {elapsed}');
    expect(text('common.goal')).toBe('Objetivo propio');
    expect(text('common.observability_legend')).toBe('Observable no significa zona despejada');
  });
});
