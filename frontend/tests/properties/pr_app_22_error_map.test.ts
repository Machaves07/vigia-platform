import fc from 'fast-check';
import { describe, expect, it } from 'vitest';

import platform from '../../../backend/resources/labels.platform.es.json';
import loop from '../../../backend/resources/labels.loop.es.json';
import { behaviourFor, counts_for_circuit } from '@/ports/errors/map';
import texts from '@/texts/errors.json';

const codes = [
  'invalid_request',
  'not_found',
  'unauthenticated',
  'second_factor_required',
  'forbidden',
  'throttled',
  'rate_limited',
  'payload_too_large',
  'conflict',
  'temporarily_unavailable',
  'internal_error',
  'privacy_notice_required',
  'period_too_long',
  'zone_without_node',
  'storage_unavailable',
] as const;
const details = Object.keys({
  ...platform.catalog_detail_code,
  ...platform.fleet_detail_code,
  ...loop.loop_detail_code,
  ...loop.accreditation_detail_code,
  ...loop.notifications_detail_code,
});
const kinds = [
  'reauth',
  'second_factor',
  'privacy_notice',
  'not_available_in_scope',
  'forbidden_reload',
  'anchored',
  'payload_too_large',
  'countdown',
  'transient',
  'degraded_section',
  'period_too_long',
  'zone_without_node',
  'internal',
];
const error = fc.record({
  code: fc.oneof(fc.constantFrom(...codes), fc.string()),
  detail_code: fc.oneof(fc.constantFrom(...details), fc.string(), fc.constant(null)),
  status: fc.integer({ min: 0, max: 599 }),
  retry_after_seconds: fc.oneof(fc.double(), fc.constant(null)),
  body: fc.jsonValue(),
});

describe('PR-APP-22: mapa cerrado y total', () => {
  it('cualquier code y detail_code obtiene exactamente un comportamiento sin exponer body', () => {
    fc.assert(
      fc.property(error, (input) => {
        const result = behaviourFor(input);
        expect(result).toBeTypeOf('object');
        expect(kinds.filter((kind) => kind === result.kind)).toHaveLength(1);
        expect(Object.hasOwn(texts, result.text_key)).toBe(true);
        expect(result.message_es).toBeTypeOf('string');
        expect(
          Object.keys(result).filter(
            (key) =>
              ![
                'kind',
                'text_key',
                'message_es',
                'preserve_draft',
                'preserve_route',
                'retry_after_seconds',
                'section',
                'export_state',
                'export_state_label',
              ].some((allowed) => key === allowed),
          ),
        ).toEqual([]);
        for (const value of Object.values(result)) {
          expect(value === null || typeof value !== 'object').toBe(true);
        }
        expect(behaviourFor(input)).toEqual(result);
      }),
    );
  });

  it('un código desconocido siempre es interno aunque tenga detalle conocido', () => {
    fc.assert(
      fc.property(
        fc.string().filter((code) => !codes.some((known) => known === code)),
        fc.constantFrom(...details),
        (code, detail_code) => {
          expect(behaviourFor({ code, detail_code, status: 503 })).toEqual({
            kind: 'internal',
            text_key: 'errors.internal_error',
            message_es: texts['errors.internal_error'],
          });
        },
      ),
    );
  });

  it('todo detalle de las cinco listas queda anclado o degrada solo la sección', () => {
    fc.assert(
      fc.property(fc.constantFrom(...details), (detail_code) => {
        for (const [code, status, kind] of [
          ['invalid_request', 400, 'anchored'],
          ['conflict', 409, 'anchored'],
          ['temporarily_unavailable', 503, 'degraded_section'],
        ] as const) {
          const input = { code, detail_code, status };
          expect(behaviourFor(input).kind).toBe(kind);
          expect(counts_for_circuit(input)).toBe(false);
        }
      }),
    );
  });

  it('el circuito excluye siempre throttled y rate_limited con cualquier detalle y estado', () => {
    fc.assert(
      fc.property(error, (input) => {
        for (const code of ['throttled', 'rate_limited']) {
          expect(counts_for_circuit({ ...input, code })).toBe(false);
        }
      }),
    );
  });
});
