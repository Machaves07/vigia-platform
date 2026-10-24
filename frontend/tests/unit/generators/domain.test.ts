/**
 * Generadores de dominio (LC-APP-24): los del contrato validan con los validadores compilados de
 * `@vigia/contracts`, los esquemas que se rehacen coinciden con los del paquete, los principales
 * siguen la matriz de U-02, y las vistas de lectura admiten campos de persona inyectados para que
 * PR-APP-07 pueda intentarlo.
 */
import {
  labelsEs,
  validateDetectionForReview,
  validateFinding,
  validateHeartbeat,
} from '@vigia/contracts';
import fc from 'fast-check';
import { isDeepStrictEqual } from 'node:util';
import { describe, expect, it } from 'vitest';

import {
  COMMON_DEFS,
  compiledSchemaConstants,
  CONTRACT_SCHEMAS_BASE,
  contractRegistry,
} from '../../generators/contracts';
import {
  API_DOCUMENT,
  BROWSER_CAPABILITIES,
  browserCapabilities,
  cacheEvents,
  circuitEvents,
  concessionEvents,
  COVERAGE_STATES,
  coverageIntervals,
  detectionForReview,
  effectivePermissions,
  finding,
  heartbeat,
  injectFields,
  liveViewDelivery,
  liveViewToken,
  localUrl,
  MAX_INTERVAL_MS,
  meResponse,
  objectPaths,
  PERMISSION_CONTEXTS,
  PERMISSION_KEYS,
  PERSON_FIELDS,
  personFields,
  principal,
  readView,
  ROLES,
  SCOPE_LEVELS,
  SCREENS,
  sessionEvents,
  type Assignment,
} from '../../generators';
import { openApiValidators } from '../../support/openapiAjv';
import { PROPERTY_TIMEOUT_MS } from '../../support/timeouts';

const validators = openApiValidators(API_DOCUMENT.components.schemas);

describe(
  'contrato: Finding, Heartbeat y DetectionForReview',
  { timeout: PROPERTY_TIMEOUT_MS },
  () => {
    it.each([
      ['Finding', finding, validateFinding],
      ['Heartbeat', heartbeat, validateHeartbeat],
      ['DetectionForReview', detectionForReview, validateDetectionForReview],
    ] as const)('%s generado valida con el validador del paquete', (_name, arbitrary, validate) => {
      fc.assert(
        fc.property(arbitrary() as fc.Arbitrary<unknown>, (value) => {
          expect(validate(value), JSON.stringify(validate.errors)).toBe(true);
        }),
      );
    });

    it('los Finding generados recorren las ramas condicionales del esquema', () => {
      const sample = fc.sample(finding(), { numRuns: 400, seed: 7 });
      expect(new Set(sample.map((value) => value.tier))).toEqual(
        new Set(Object.keys(labelsEs.evidence_tier)),
      );
      expect(sample.some((value) => value.cameras.length > 1)).toBe(true);
      for (const value of sample) {
        expect(value.cameras.filter((camera) => camera.originating).length).toBe(1);
      }
      expect(sample.some((value) => value.episode.segment_index === 0)).toBe(true);
      expect(sample.some((value) => value.episode.segment_index > 0)).toBe(true);
    });

    it('cada definición rehecha de common.schema.json es idéntica a una constante del paquete', () => {
      const constants = compiledSchemaConstants();
      for (const [name, definition] of Object.entries(COMMON_DEFS)) {
        expect(
          constants.some((constant) => isDeepStrictEqual(constant, definition)),
          `common.schema.json#/$defs/${name} no coincide con @vigia/contracts`,
        ).toBe(true);
      }
    });

    it('enumerations.schema.json se rehace desde labelsEs y cubre toda referencia de los documentos', () => {
      const registry = contractRegistry();
      const enumerations = registry[`${CONTRACT_SCHEMAS_BASE}enumerations.schema.json`] as {
        $defs: Record<string, { enum: string[] }>;
      };
      const used = new Set<string>();
      const walk = (value: unknown): void => {
        if (Array.isArray(value)) value.forEach(walk);
        else if (typeof value === 'object' && value !== null) {
          for (const [key, item] of Object.entries(value)) {
            if (
              key === '$ref' &&
              typeof item === 'string' &&
              item.startsWith('enumerations.schema.json#/$defs/')
            ) {
              used.add(item.slice('enumerations.schema.json#/$defs/'.length));
            } else walk(item);
          }
        }
      };
      for (const name of [
        'finding',
        'heartbeat',
        'detection_for_review',
        'receipt',
        'clip_reference',
      ]) {
        expect(registry[`${CONTRACT_SCHEMAS_BASE}${name}.schema.json`], name).toBeDefined();
        walk(registry[`${CONTRACT_SCHEMAS_BASE}${name}.schema.json`]);
      }
      walk(COMMON_DEFS);
      expect(used.size).toBeGreaterThan(0);
      for (const list of used) {
        expect(enumerations.$defs[list]?.enum, list).toEqual(
          Object.keys(labelsEs[list as keyof typeof labelsEs]),
        );
      }
    });
  },
);

describe('principal y asignaciones (matriz de U-02)', { timeout: PROPERTY_TIMEOUT_MS }, () => {
  it('los permisos efectivos son la unión de las columnas del contexto', () => {
    fc.assert(
      fc.property(principal(), (subject) => {
        expect(subject.effectivePermissions).toEqual(
          effectivePermissions(
            subject.context,
            subject.assignments.map((a) => a.role),
          ),
        );
        expect(subject.concessionId === null).toBe(subject.context !== 'customer_under_concession');
        for (const assignment of subject.assignments) {
          if (assignment.scope_level === 'organization')
            expect(assignment.scope_id).toBe(subject.organizationId);
        }
      }),
    );
  });

  it('roles, niveles y claves coinciden con app.yaml y u05-fixtures.json', () => {
    const schemas = API_DOCUMENT.components.schemas as Record<string, { enum?: unknown }>;
    expect([...ROLES].sort()).toEqual([...(schemas.Role?.enum as string[])].sort());
    expect([...SCOPE_LEVELS]).toEqual(schemas.ScopeLevel?.enum);
    for (const context of PERMISSION_CONTEXTS) {
      for (const key of effectivePermissions(context, ROLES))
        expect(PERMISSION_KEYS).toContain(key);
    }
    const assignments: readonly Assignment[] = fc
      .sample(principal(), { numRuns: 50, seed: 9 })
      .flatMap((subject) => subject.assignments);
    expect(new Set(assignments.map((a) => a.role))).toEqual(new Set(ROLES));
  });

  it('oráculo de la matriz: ejemplos de u05-fixtures.json', () => {
    expect(effectivePermissions('customer_organization', ['provider_installer'])).toEqual([]);
    expect(effectivePermissions('customer_under_concession', ['coordinator_sst'])).toEqual([]);
    expect(effectivePermissions('provider_organization', ['platform_operator'])).toContain(
      'platform.keys.rotate',
    );
    expect(effectivePermissions('customer_organization', ['copasst'])).toEqual([
      'catalog.read',
      'coverage.read',
      'hierarchy.read',
      'live_view.open',
      'notifications.read',
      'transparency.read',
    ]);
    expect(PERMISSION_CONTEXTS).toEqual([
      'customer_organization',
      'provider_organization',
      'customer_under_concession',
    ]);
    expect(() => effectivePermissions('customer_organization', ['nadie'])).toThrow(
      /Rol desconocido/,
    );
  });

  it('meResponse valida contra MeResponse y repite el principal', () => {
    const validate = validators.component('MeResponse');
    fc.assert(
      fc.property(meResponse(), ({ principal: subject, me }) => {
        expect(validate(me), JSON.stringify(validate.errors)).toBe(true);
        expect(me.effective_permissions).toEqual(subject.effectivePermissions);
        expect(me.concession_id).toBe(subject.concessionId);
        expect((me.organization as { organization_id: string }).organization_id).toBe(
          subject.organizationId,
        );
      }),
    );
  });
});

describe(
  'intervalos, secuencias, capacidades, tokens y URL',
  { timeout: PROPERTY_TIMEOUT_MS },
  () => {
    it('coverageIntervals: contiguos, de 1 ms a 31 días, válidos como IntervalOut', () => {
      const validate = validators.component('IntervalOut');
      fc.assert(
        fc.property(coverageIntervals({ minLength: 1 }), (intervals) => {
          intervals.forEach((interval, index) => {
            expect(validate(interval), JSON.stringify(validate.errors)).toBe(true);
            const start = Date.parse(String(interval.starts_at));
            const end = Date.parse(String(interval.ends_at));
            expect(end - start).toBe(interval.duration_ms);
            expect(interval.duration_ms).toBeGreaterThanOrEqual(1);
            expect(interval.duration_ms).toBeLessThanOrEqual(MAX_INTERVAL_MS);
            expect(COVERAGE_STATES).toContain(interval.state);
            if (index > 0) expect(interval.starts_at).toBe(intervals[index - 1]?.ends_at);
          });
        }),
      );
    });

    it.each([
      ['sesión', sessionEvents()],
      ['circuito', circuitEvents()],
      ['caché', cacheEvents()],
      ['concesión', concessionEvents()],
    ] as const)('secuencias de %s con instantes crecientes', (_name, arbitrary) => {
      fc.assert(
        fc.property(
          arbitrary as fc.Arbitrary<readonly { at: number; kind: string }[]>,
          (events) => {
            events.forEach((event, index) => {
              expect(event.at).toBeGreaterThanOrEqual(
                index === 0 ? 0 : (events[index - 1]?.at ?? 0),
              );
            });
          },
        ),
      );
    });

    it('las secuencias de caché y concesión solo llevan los campos de su clase', () => {
      expect(SCREENS).toHaveLength(16);
      expect(SCREENS[0]).toBe('SCR-01');
      for (const event of fc.sample(cacheEvents(), { numRuns: 50, seed: 3 }).flat()) {
        expect('resource' in event).toBe(event.kind === 'write');
        expect(SCREENS).toContain(event.screen);
      }
      for (const event of fc.sample(concessionEvents(), { numRuns: 50, seed: 3 }).flat()) {
        expect('concessionId' in event).toBe(event.kind === 'select' || event.kind === 'change');
      }
    });

    it('browserCapabilities recorre todos los subconjuntos de capacidades', () => {
      const sample = fc.sample(browserCapabilities(), { numRuns: 400, seed: 5 });
      expect(Object.keys(sample[0] ?? {}).sort()).toEqual([...BROWSER_CAPABILITIES].sort());
      expect(sample.some((caps) => Object.values(caps).every(Boolean))).toBe(true);
      expect(sample.some((caps) => !Object.values(caps).every(Boolean))).toBe(true);
    });

    it('liveViewDelivery: LiveViewTokenOut válido, token base64url y URL https sin token', () => {
      const validate = validators.component('LiveViewTokenOut');
      fc.assert(
        fc.property(liveViewDelivery(), (delivery) => {
          expect(validate(delivery), JSON.stringify(validate.errors)).toBe(true);
          expect(delivery.token).toMatch(/^[A-Za-z0-9_-]{32,64}$/);
          const url = new URL(String(delivery.live_view_local_url));
          expect(url.protocol).toBe('https:');
          expect(String(delivery.live_view_local_url)).not.toContain(String(delivery.token));
        }),
      );
    });

    it('liveViewToken y localUrl por separado: base64url y https con host, puerto y ruta válidos', () => {
      fc.assert(
        fc.property(liveViewToken(), localUrl(), (token, url) => {
          expect(token).toMatch(/^[A-Za-z0-9_-]{32,64}$/);
          const parsed = new URL(url);
          expect(parsed.protocol).toBe('https:');
          expect(parsed.search).toBe('');
          expect(parsed.hash).toBe('');
          expect(url).toMatch(/^https:\/\/[A-Za-z0-9.-]+(:[0-9]{1,5})?(\/[A-Za-z0-9._~-]+)*\/?$/);
        }),
      );
    });
  },
);

describe(
  'vistas de lectura con campos de persona inyectables (PR-APP-07)',
  { timeout: PROPERTY_TIMEOUT_MS },
  () => {
    it('sin inyección, la vista valida contra su esquema y no trae campos de persona', () => {
      const validate = validators.component('FindingDetailOut');
      fc.assert(
        fc.property(readView('FindingDetailOut'), (view) => {
          expect(validate(view), JSON.stringify(validate.errors)).toBe(true);
          for (const path of objectPaths(view)) {
            const node = path.reduce<unknown>(
              (value, key) => (value as Record<string, unknown>)[String(key)],
              view,
            );
            for (const field of PERSON_FIELDS)
              expect(Object.hasOwn(node as object, field)).toBe(false);
          }
        }),
      );
    });

    it.each(PERSON_FIELDS)('inyecta %s en la raíz y la vista deja de validar', (field) => {
      const validate = validators.component('FindingDetailOut');
      const [view] = fc.sample(
        readView('FindingDetailOut', { inject: fc.constant({ [field]: 'p-0001' }) }),
        {
          numRuns: 1,
          seed: 11,
        },
      );
      expect(view?.[field]).toBe('p-0001');
      expect(validate(view)).toBe(false);
    });

    it('con where: anywhere los campos llegan también a objetos anidados', () => {
      const sample = fc.sample(
        readView('FindingDetailOut', { inject: personFields(), where: 'anywhere' }),
        { numRuns: 200, seed: 13 },
      );
      const injectedAt = sample.map((view) =>
        objectPaths(view).find((path) => {
          const node = path.reduce<unknown>(
            (value, key) => (value as Record<string, unknown>)[String(key)],
            view,
          );
          return PERSON_FIELDS.some((field) => Object.hasOwn(node as object, field));
        }),
      );
      expect(injectedAt.every((path) => path !== undefined)).toBe(true);
      expect(injectedAt.some((path) => (path?.length ?? 0) > 0)).toBe(true);
    });

    it('personFields da un subconjunto no vacío de los tres campos', () => {
      const sample = fc.sample(personFields(), { numRuns: 300, seed: 17 });
      for (const fields of sample) {
        expect(Object.keys(fields).length).toBeGreaterThan(0);
        expect(
          Object.keys(fields).every((key) => (PERSON_FIELDS as readonly string[]).includes(key)),
        ).toBe(true);
      }
      for (const field of PERSON_FIELDS)
        expect(sample.some((fields) => field in fields)).toBe(true);
    });

    it('injectFields no modifica el original y rechaza inyectar en algo que no es objeto', () => {
      const original = { a: [{ b: 1 }] };
      expect(injectFields(original, { face: 'x' }, ['a', 0])).toEqual({ a: [{ b: 1, face: 'x' }] });
      expect(original).toEqual({ a: [{ b: 1 }] });
      expect(() => injectFields([1], { face: 'x' })).toThrow(
        /Solo se inyectan campos en un objeto/,
      );
    });
  },
);
