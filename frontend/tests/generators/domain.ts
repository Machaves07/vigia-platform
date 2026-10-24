/**
 * Generadores de dominio (LC-APP-24) para las propiedades PR-APP-01 a 30.
 *
 * - `finding`, `heartbeat` y `detectionForReview`: desde los esquemas de `@vigia/contracts`
 *   (`contracts.ts`), válidos según los validadores compilados del paquete (PR-APP-19).
 * - `principal` y `meResponse`: asignaciones (rol, nivel, alcance) y permisos efectivos según la
 *   matriz de U-02 exportada en `backend/openapi/u05-fixtures.json` (PR-APP-01, 02).
 * - `coverageIntervals`: intervalos contiguos de 1 ms a 31 días (PR-APP-05, 27).
 * - Secuencias de eventos de sesión, circuito, caché y concesión (PR-APP-04, 16, 18, 21, 23, 24),
 *   con instantes relativos en milisegundos (nunca la hora del sistema).
 * - `browserCapabilities` (PR-APP-26), `liveViewDelivery` (PR-APP-20, 29).
 * - `readView`: vistas de lectura de `app.yaml` con campos extra inyectables, por ejemplo los de
 *   persona de `personFields` (PR-APP-07).
 *
 * Solo dependen de lo dibujado: ni hora del sistema ni estado.
 */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import type { DetectionForReview, Finding, Heartbeat } from '@vigia/contracts';
import { labelsEs } from '@vigia/contracts';
import fc from 'fast-check';

import { contractRegistry, contractSchema } from './contracts';
import { fromSchema } from './fromSchema';
import { componentArbitrary } from './openapi';

// --- Contrato -------------------------------------------------------------------------------

const contractArbitraries = new Map<string, fc.Arbitrary<unknown>>();

function contractArbitrary(name: string): fc.Arbitrary<unknown> {
  let arbitrary = contractArbitraries.get(name);
  if (arbitrary === undefined) {
    arbitrary = fromSchema(contractSchema(name) as Record<string, unknown>, {
      registry: contractRegistry(),
    });
    contractArbitraries.set(name, arbitrary);
  }
  return arbitrary;
}

/** `Finding` del contrato (con `receipt`), válido según `validateFinding`. */
export function finding(): fc.Arbitrary<Finding> {
  return contractArbitrary('finding') as fc.Arbitrary<Finding>;
}

/** `Heartbeat` del contrato, válido según `validateHeartbeat`. */
export function heartbeat(): fc.Arbitrary<Heartbeat> {
  return contractArbitrary('heartbeat') as fc.Arbitrary<Heartbeat>;
}

/** `DetectionForReview` del contrato (con `receipt`), válido según `validateDetectionForReview`. */
export function detectionForReview(): fc.Arbitrary<DetectionForReview> {
  return contractArbitrary('detection_for_review') as fc.Arbitrary<DetectionForReview>;
}

// --- Principal y asignaciones (matriz de U-02) ----------------------------------------------

interface PermissionFixtures {
  readonly keys: readonly string[];
  readonly roles: readonly string[];
  readonly effective: Readonly<Record<string, Readonly<Record<string, readonly string[]>>>>;
}

function loadPermissions(): PermissionFixtures {
  const path = resolve(
    dirname(fileURLToPath(import.meta.url)),
    '../../../backend/openapi/u05-fixtures.json',
  );
  const fixtures = JSON.parse(readFileSync(path, 'utf8')) as {
    format?: unknown;
    permissions?: PermissionFixtures;
  };
  if (fixtures.format !== 'vigia-platform/u05-fixtures' || fixtures.permissions === undefined) {
    throw new Error('backend/openapi/u05-fixtures.json: formato inesperado.');
  }
  return fixtures.permissions;
}

const PERMISSIONS = loadPermissions();

/** Contextos en que la API evalúa la matriz de forma distinta (`u05-fixtures.json`). */
export type PermissionContext =
  'customer_organization' | 'provider_organization' | 'customer_under_concession';

export const PERMISSION_CONTEXTS = Object.keys(PERMISSIONS.effective) as PermissionContext[];
export const ROLES: readonly string[] = PERMISSIONS.roles;
export const PERMISSION_KEYS: readonly string[] = PERMISSIONS.keys;
export const SCOPE_LEVELS = ['organization', 'plant', 'zone'] as const;

export interface Assignment {
  readonly role: string;
  readonly scope_level: (typeof SCOPE_LEVELS)[number];
  readonly scope_id: string;
}

export interface Principal {
  readonly context: PermissionContext;
  readonly organizationId: string;
  readonly concessionId: string | null;
  readonly assignments: readonly Assignment[];
  /** Unión ordenada de las claves que conceden las asignaciones en el contexto (oráculo). */
  readonly effectivePermissions: readonly string[];
}

/** Claves que la matriz de U-02 concede a `roles` en `context` (oráculo de PR-APP-01). */
export function effectivePermissions(
  context: PermissionContext,
  roles: Iterable<string>,
): readonly string[] {
  const columns = PERMISSIONS.effective[context];
  if (columns === undefined) throw new Error(`Contexto desconocido: ${context}.`);
  const keys = new Set<string>();
  for (const role of roles) {
    const granted = columns[role];
    if (granted === undefined) throw new Error(`Rol desconocido: ${role}.`);
    for (const key of granted) keys.add(key);
  }
  return [...keys].sort();
}

/** Principal con 1 a 4 asignaciones y sus permisos efectivos según la matriz. */
export function principal(
  options: { readonly context?: PermissionContext } = {},
): fc.Arbitrary<Principal> {
  const context =
    options.context === undefined
      ? fc.constantFrom(...PERMISSION_CONTEXTS)
      : fc.constant(options.context);
  return fc
    .record({ context, organizationId: fc.uuid(), concession: fc.uuid() })
    .chain(({ context: chosen, organizationId, concession }) =>
      fc
        .array(
          fc.record({
            role: fc.constantFrom(...ROLES),
            scope_level: fc.constantFrom(...SCOPE_LEVELS),
            scope_id: fc.uuid(),
          }),
          { minLength: 1, maxLength: 4 },
        )
        .map((assignments) => ({
          context: chosen,
          organizationId,
          concessionId: chosen === 'customer_under_concession' ? concession : null,
          assignments: assignments.map((assignment) =>
            assignment.scope_level === 'organization'
              ? { ...assignment, scope_id: organizationId }
              : assignment,
          ),
          effectivePermissions: effectivePermissions(
            chosen,
            assignments.map((assignment) => assignment.role),
          ),
        })),
    );
}

/** `GET /me` (`MeResponse` de `app.yaml`) coherente con un `principal` generado. */
export function meResponse(
  options: { readonly context?: PermissionContext } = {},
): fc.Arbitrary<{ readonly principal: Principal; readonly me: Record<string, unknown> }> {
  return fc
    .tuple(
      principal(options),
      componentArbitrary('MeResponse') as fc.Arbitrary<Record<string, unknown>>,
    )
    .map(([subject, me]) => {
      const organization = me.organization as Record<string, unknown>;
      return {
        principal: subject,
        me: {
          ...me,
          organization: { ...organization, organization_id: subject.organizationId },
          assignments: subject.assignments.map((assignment) => ({ ...assignment })),
          effective_permissions: [...subject.effectivePermissions],
          concession_id: subject.concessionId,
        },
      };
    });
}

// --- Intervalos de cobertura ----------------------------------------------------------------

export const MAX_INTERVAL_MS = 31 * 24 * 60 * 60 * 1000;

/** Los seis estados de cobertura que suma `SummaryOut` (PR-APP-05). */
export const COVERAGE_STATES = [
  'observable',
  'degraded',
  'not_observable',
  'no_communication',
  'never_reported',
  'zone_not_active',
] as const;

const EPOCH_MIN = Date.UTC(2026, 0, 1);
const EPOCH_MAX = Date.UTC(2027, 11, 31);

/**
 * Intervalos `IntervalOut` contiguos (cada uno empieza donde acaba el anterior), de 1 ms a
 * 31 días, con `duration_ms = ends_at − starts_at` y un estado de `COVERAGE_STATES`.
 */
export function coverageIntervals(
  options: { readonly minLength?: number; readonly maxLength?: number } = {},
): fc.Arbitrary<Record<string, unknown>[]> {
  const shape = componentArbitrary('IntervalOut') as fc.Arbitrary<Record<string, unknown>>;
  const causes = Object.keys(labelsEs.degradation_cause);
  const interval = fc.record({
    base: shape,
    durationMs: fc.integer({ min: 1, max: MAX_INTERVAL_MS }),
    state: fc.constantFrom(...COVERAGE_STATES),
    causes: fc.uniqueArray(fc.constantFrom(...causes), { maxLength: 3 }),
  });
  return fc
    .tuple(
      fc.integer({ min: EPOCH_MIN, max: EPOCH_MAX }),
      fc.array(interval, { minLength: options.minLength ?? 0, maxLength: options.maxLength ?? 12 }),
    )
    .map(([start, parts]) => {
      let cursor = start;
      return parts.map(({ base, durationMs, state, causes: chosen }) => {
        const startsAt = cursor;
        cursor += durationMs;
        return {
          ...base,
          starts_at: new Date(startsAt).toISOString(),
          ends_at: new Date(cursor).toISOString(),
          duration_ms: durationMs,
          state,
          causes: state === 'degraded' ? chosen : [],
        };
      });
    });
}

// --- Secuencias de eventos ------------------------------------------------------------------

/** Instantes relativos crecientes (ms desde 0) para `count` eventos, con pasos de hasta `maxStepMs`. */
function instants(maxStepMs: number, maxLength: number): fc.Arbitrary<number[]> {
  return fc.array(fc.integer({ min: 0, max: maxStepMs }), { maxLength }).map((steps) => {
    let at = 0;
    return steps.map((step) => (at += step));
  });
}

function timed<T>(event: fc.Arbitrary<T>, maxStepMs: number, maxLength: number) {
  return instants(maxStepMs, maxLength).chain((times) =>
    fc
      .tuple(...times.map(() => event))
      .map((events) => events.map((value, index) => ({ ...value, at: times[index] ?? 0 }))),
  );
}

export type SessionEvent =
  | { readonly kind: 'response'; readonly at: number }
  | { readonly kind: 'user_action'; readonly at: number }
  | { readonly kind: 'tick'; readonly at: number };

/** Respuestas autenticadas, acciones y tics, con pasos de hasta 40 min (PR-APP-04). */
export function sessionEvents(maxLength = 40): fc.Arbitrary<SessionEvent[]> {
  return timed(
    fc.record({
      kind: fc.constantFrom('response' as const, 'user_action' as const, 'tick' as const),
    }),
    40 * 60 * 1000,
    maxLength,
  );
}

export type CircuitEvent =
  | {
      readonly kind: 'success' | 'transient_failure' | 'rate_limited';
      readonly dependency: string;
      readonly at: number;
    }
  | { readonly kind: 'tick'; readonly dependency: string; readonly at: number };

/** Resultados de puertos por dependencia y tics de reloj, con pasos de hasta 45 s (PR-APP-18, 23). */
export function circuitEvents(
  dependencies: readonly string[] = ['platform', 'storage', 'assistant'],
  maxLength = 60,
): fc.Arbitrary<CircuitEvent[]> {
  return timed(
    fc.record({
      kind: fc.constantFrom(
        'success' as const,
        'transient_failure' as const,
        'rate_limited' as const,
        'tick' as const,
      ),
      dependency: fc.constantFrom(...dependencies),
    }),
    45 * 1000,
    maxLength,
  );
}

export const SCREENS = Array.from(
  { length: 16 },
  (_, index) => `SCR-${String(index + 1).padStart(2, '0')}`,
);

export type CacheEvent =
  | { readonly kind: 'navigate' | 'refresh'; readonly screen: string; readonly at: number }
  | {
      readonly kind: 'write';
      readonly screen: string;
      readonly resource: string;
      readonly at: number;
    }
  | { readonly kind: 'tick'; readonly screen: string; readonly at: number };

/** Navegación, refrescos explícitos, escrituras y tics (PR-APP-16, 24). */
export function cacheEvents(
  resources: readonly string[] = ['findings', 'zones', 'nodes', 'concessions', 'notifications'],
  maxLength = 40,
): fc.Arbitrary<CacheEvent[]> {
  return timed(
    fc.record({
      kind: fc.constantFrom(
        'navigate' as const,
        'refresh' as const,
        'write' as const,
        'tick' as const,
      ),
      screen: fc.constantFrom(...SCREENS),
      resource: fc.constantFrom(...resources),
    }),
    5 * 60 * 1000,
    maxLength,
  ).map((events) =>
    events.map(({ resource, ...event }): CacheEvent =>
      event.kind === 'write'
        ? { ...event, kind: 'write', resource }
        : { ...event, kind: event.kind },
    ),
  );
}

export type ConcessionEvent =
  | { readonly kind: 'select' | 'change'; readonly concessionId: string; readonly at: number }
  | { readonly kind: 'expire' | 'clear' | 'request'; readonly at: number };

/** Selección, cambio, vencimiento y fin de concesiones, y peticiones intercaladas (PR-APP-21). */
export function concessionEvents(maxLength = 30): fc.Arbitrary<ConcessionEvent[]> {
  return timed(
    fc.record({
      kind: fc.constantFrom(
        'select' as const,
        'change' as const,
        'expire' as const,
        'clear' as const,
        'request' as const,
      ),
      concessionId: fc.uuid(),
    }),
    60 * 60 * 1000,
    maxLength,
  ).map((events) =>
    events.map(({ concessionId, ...event }): ConcessionEvent =>
      event.kind === 'select' || event.kind === 'change'
        ? { ...event, kind: event.kind, concessionId }
        : { ...event, kind: event.kind },
    ),
  );
}

// --- Capacidades del navegador, tokens y URL ------------------------------------------------

/** Capacidades que comprueba el arranque (NFR-APP-31; PAT-APP-REN de `nfr-design-patterns.md` §3). */
export const BROWSER_CAPABILITIES = [
  'fetch',
  'intl_time_zones',
  'crypto_subtle',
  'video_h264',
  'sec_fetch_site',
] as const;

export type BrowserCapabilities = Readonly<Record<(typeof BROWSER_CAPABILITIES)[number], boolean>>;

/** Cualquier subconjunto de capacidades presentes (PR-APP-26). */
export function browserCapabilities(): fc.Arbitrary<BrowserCapabilities> {
  return fc.record(
    Object.fromEntries(BROWSER_CAPABILITIES.map((name) => [name, fc.boolean()])) as Record<
      (typeof BROWSER_CAPABILITIES)[number],
      fc.Arbitrary<boolean>
    >,
  );
}

/** Token de vista en vivo: base64url de 32 a 64 caracteres. */
export function liveViewToken(): fc.Arbitrary<string> {
  return fc.stringMatching(/^[A-Za-z0-9_-]{32,64}$/);
}

/** URL local del nodo (`https://host[:puerto]/ruta`), sin token. */
export function localUrl(): fc.Arbitrary<string> {
  return fc
    .record({
      host: fc.oneof(
        fc.domain(),
        fc
          .tuple(...Array.from({ length: 4 }, () => fc.integer({ min: 1, max: 254 })))
          .map((p) => p.join('.')),
      ),
      port: fc.option(fc.integer({ min: 1, max: 65535 }), { nil: undefined }),
      path: fc.array(fc.stringMatching(/^[A-Za-z0-9._~-]{1,12}$/), { maxLength: 3 }),
    })
    .map(
      ({ host, port, path }) =>
        `https://${host}${port === undefined ? '' : `:${String(port)}`}/${path.join('/')}`,
    );
}

/** Entrega de vista en vivo (`LiveViewTokenOut`) con token y URL local generados (PR-APP-20, 29). */
export function liveViewDelivery(): fc.Arbitrary<Record<string, unknown>> {
  return fc
    .tuple(
      componentArbitrary('LiveViewTokenOut') as fc.Arbitrary<Record<string, unknown>>,
      liveViewToken(),
      localUrl(),
    )
    .map(([delivery, token, url]) => ({ ...delivery, token, live_view_local_url: url }));
}

// --- Vistas de lectura con campos inyectables (PR-APP-07) ----------------------------------

/** Campos de persona que ninguna vista de lectura tiene (BR-APP-21): PR-APP-07 los inyecta. */
export const PERSON_FIELDS = ['person_id', 'track_id', 'face'] as const;

/** Un subconjunto no vacío de `PERSON_FIELDS` con valores plausibles. */
export function personFields(): fc.Arbitrary<Record<string, unknown>> {
  return fc
    .record(
      {
        person_id: fc.oneof(fc.uuid(), fc.integer({ min: 1, max: 9999 })),
        track_id: fc.oneof(
          fc.integer({ min: 0, max: 99999 }),
          fc.stringMatching(/^trk-[0-9]{1,6}$/),
        ),
        face: fc.oneof(
          fc.base64String({ minLength: 8, maxLength: 64 }),
          fc.constant({ x: 0, y: 0, w: 1, h: 1 }),
        ),
      },
      { requiredKeys: [] },
    )
    .filter((fields) => Object.keys(fields).length > 0);
}

type JsonPath = readonly (string | number)[];

/** Rutas de todos los objetos de `value` (la raíz es `[]`). */
export function objectPaths(value: unknown, path: JsonPath = []): JsonPath[] {
  if (Array.isArray(value))
    return value.flatMap((item, index) => objectPaths(item, [...path, index]));
  if (typeof value !== 'object' || value === null) return [];
  return [
    path,
    ...Object.entries(value as Record<string, unknown>).flatMap(([key, item]) =>
      objectPaths(item, [...path, key]),
    ),
  ];
}

/** Copia de `value` con `fields` añadidos al objeto de la ruta `path`. */
export function injectFields(
  value: unknown,
  fields: Readonly<Record<string, unknown>>,
  path: JsonPath = [],
): unknown {
  const [head, ...rest] = path;
  if (head === undefined) {
    if (typeof value !== 'object' || value === null || Array.isArray(value)) {
      throw new Error('Solo se inyectan campos en un objeto.');
    }
    return { ...(value as Record<string, unknown>), ...fields };
  }
  if (Array.isArray(value)) {
    return (value as unknown[]).map((item, index) =>
      index === head ? injectFields(item, fields, rest) : item,
    );
  }
  const record = value as Record<string, unknown>;
  return { ...record, [head]: injectFields(record[String(head)], fields, rest) };
}

export interface ReadViewOptions {
  /** Campos extra que se añaden a la vista (por ejemplo, `personFields()`). */
  readonly inject?: fc.Arbitrary<Readonly<Record<string, unknown>>>;
  /** `root` (por omisión) los añade a la raíz; `anywhere`, a cualquier objeto anidado. */
  readonly where?: 'root' | 'anywhere';
}

/**
 * Vista de lectura `components.schemas.<schemaName>` de `app.yaml`, opcionalmente con campos
 * extra inyectados para que PR-APP-07 compruebe que ninguna pantalla los muestra.
 */
export function readView(
  schemaName: string,
  options: ReadViewOptions = {},
): fc.Arbitrary<Record<string, unknown>> {
  const view = componentArbitrary(schemaName) as fc.Arbitrary<Record<string, unknown>>;
  const inject = options.inject;
  if (inject === undefined) return view;
  return fc.tuple(view, inject, fc.nat()).map(([value, fields, pick]) => {
    const paths = options.where === 'anywhere' ? objectPaths(value) : [[]];
    const path = paths[pick % paths.length] ?? [];
    return injectFields(value, fields, path) as Record<string, unknown>;
  });
}
