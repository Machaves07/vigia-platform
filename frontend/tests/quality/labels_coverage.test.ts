import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import fc from 'fast-check';
import { describe, expect, it } from 'vitest';

import {
  contractEnums,
  loadEnumerations,
  loadLabels,
  mergeLabels,
  missingLabels,
  openApiEnums,
} from '../../scripts/lib/labels_catalog.mjs';
import { label } from '@/texts/labels';
import { text } from '@/texts';

const root = process.cwd();
const resources = join(root, '../backend/resources');

describe('cobertura de etiquetas por conjuntos (NFR-APP-52)', () => {
  it('cubre cada valor OpenAPI y cada unión literal del contrato fijado', () => {
    const enums = loadEnumerations(root);
    expect(enums.length).toBeGreaterThan(0);
    expect(missingLabels(enums, loadLabels(root, resources))).toEqual([]);
  });

  it('descubre enums nuevos, inline y anidados sin depender de las etiquetas', () => {
    const enums = openApiEnums({
      components: {
        schemas: {
          FutureState: { enum: ['new', 'old'] },
          Response: { properties: { state: { anyOf: [{ enum: ['pending', 'done'] }] } } },
        },
      },
    });
    expect(enums.map((entry) => entry.values)).toEqual([
      ['new', 'old'],
      ['pending', 'done'],
    ]);
    expect(
      missingLabels(enums.slice(0, 1), { future_state: { new: 'Nuevo', wrong: 'Otro' } }),
    ).toEqual(['components.schemas.FutureState: future_state.old sin etiqueta']);
  });

  it('lee las uniones multilinea del AST del contrato, no los valores de su catálogo', () => {
    const enums = contractEnums('export type FutureState =\n "new" |\n "old";');
    expect(enums).toEqual([
      { name: 'future_state', values: ['new', 'old'], origin: 'contract.FutureState' },
    ]);
    expect(missingLabels(enums, { future_state: { new: 'Nuevo' } })).toEqual([
      'contract.FutureState: future_state.old sin etiqueta',
    ]);
    expect(() => contractEnums('export type FutureState = string;')).toThrow();
  });

  it('rechaza etiquetas vacías, espacios y claves heredadas', () => {
    expect(
      missingLabels([{ name: 'future', values: ['blank', 'space', 'toString'], origin: 'probe' }], {
        future: { blank: '', space: ' ' },
      }),
    ).toEqual([
      'probe: future.blank sin etiqueta',
      'probe: future.space sin etiqueta',
      'probe: future.toString sin etiqueta',
    ]);
  });

  it('combina por valor los nombres compartidos sin perder valores de U-01', () => {
    expect(mergeLabels([{ shared: { one: 'Uno' } }, { shared: { two: 'Dos' } }])).toEqual({
      shared: { one: 'Uno', two: 'Dos' },
    });
    expect(label('verification_result', 'passed')).toBe('superada');
    expect(label('verification_result', 'intact')).toBe('íntegra');
    expect(label('circuit_state', 'half_open')).toBe('a prueba');
  });

  it('quitar una etiqueta de labels.loop.es.json en una copia pone el comando en rojo', () => {
    const cache = join(root, 'node_modules/.cache/vig287');
    mkdirSync(cache, { recursive: true });
    const copy = mkdtempSync(join(cache, 'labels-'));
    try {
      const loop = JSON.parse(
        readFileSync(join(resources, 'labels.loop.es.json'), 'utf8'),
      ) as Record<string, Record<string, string>>;
      delete loop.finding_lifecycle_state?.received;
      writeFileSync(join(copy, 'labels.loop.es.json'), JSON.stringify(loop));
      writeFileSync(
        join(copy, 'labels.platform.es.json'),
        readFileSync(join(resources, 'labels.platform.es.json')),
      );
      const result = spawnSync(process.execPath, [join(root, 'scripts/checks/labels.mjs'), copy], {
        encoding: 'utf8',
        timeout: 60_000,
      });
      expect(result.status).toBe(1);
      expect(result.stderr.trim().split('\n')).toEqual([
        'components.schemas.FindingLifecycleState: finding_lifecycle_state.received sin etiqueta',
      ]);
    } finally {
      rmSync(copy, { recursive: true, force: true });
    }
  });
});

describe('label: nunca valor crudo', () => {
  it('resuelve U-01, U-02, U-03, U-04 y los enums inline de la aplicación', () => {
    expect(label('observability_state', 'observable')).toBe('observable');
    expect(label('role', 'coordinator_sst')).toBe('coordinador SST');
    expect(label('admission_result', 'admitted')).toBe('admitida');
    expect(label('finding_lifecycle_state', 'received')).toBe('recibido, sin clasificar');
    expect(label('alarm_status', 'cleared')).toBe('Alarma resuelta');
  });

  it.each(['future_value', '', '__proto__', 'constructor', 'toString'])(
    'valor desconocido %s',
    (value) => {
      expect(label('observability_state', value)).toBe(text('common.unknown'));
      expect(label(value, 'observable')).toBe(text('common.unknown'));
    },
  );

  it('cualquier valor de una lista futura usa la clave común', () => {
    fc.assert(
      fc.property(fc.string(), (value) => {
        expect(label('future_enum', value)).toBe(text('common.unknown'));
      }),
    );
  });
});
