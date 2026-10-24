/**
 * Esquemas del contrato (`@vigia/contracts` v1.1.0) para los generadores de dominio (LC-APP-24).
 *
 * El paquete npm no publica `schemas/*.schema.json`: solo los validadores compilados por Ajv en
 * modo autónomo (`dist/validators/compiled.js`), que llevan dentro, como constantes JSON, cada
 * documento raíz (con su `$id`) y cada definición referenciada. Este módulo:
 *
 * - lee los documentos raíz del paquete instalado (`finding`, `heartbeat`,
 *   `detection_for_review`, `receipt`, `clip_reference`…), sin copiarlos al repositorio, que es
 *   público mientras `vigia-contracts` es privado;
 * - rehace `enumerations.schema.json` desde `labelsEs` (una lista por enumeración, con los mismos
 *   valores: lo comprueba `domain.test.ts`);
 * - reescribe las catorce definiciones de `common.schema.json`, formatos y formas que ya publican
 *   `app.yaml` y `tests/fixtures/synthetic/contracts/findings.json`; `domain.test.ts` comprueba
 *   que cada una es idéntica a una constante del paquete, así que no pueden divergir.
 *
 * Solo para Node (vitest): lee el paquete con `node:fs`.
 */
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';

import { labelsEs } from '@vigia/contracts';

export const CONTRACT_SCHEMAS_BASE = 'https://vigia.local/schemas/';

const TIMESTAMP_PATTERN =
  '^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\\.[0-9]{3}Z$';
const SEMVER_PATTERN =
  '^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)(-(0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)' +
  '(\\.(0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?(\\+[0-9A-Za-z-]+(\\.[0-9A-Za-z-]+)*)?$';

/** Las definiciones de `common.schema.json` v1.1.0, sin anotaciones. */
export const COMMON_DEFS: Readonly<Record<string, unknown>> = {
  Timestamp: {
    type: 'string',
    format: 'date-time',
    minLength: 24,
    maxLength: 24,
    pattern: TIMESTAMP_PATTERN,
  },
  UUID: {
    type: 'string',
    format: 'uuid',
    minLength: 36,
    maxLength: 36,
    pattern: '^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$',
  },
  UUIDv7: {
    type: 'string',
    format: 'uuid',
    minLength: 36,
    maxLength: 36,
    pattern: '^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$',
  },
  Code: { type: 'string', minLength: 2, maxLength: 32, pattern: '^[A-Z0-9-]{2,32}$' },
  TechnicalId: {
    type: 'string',
    minLength: 1,
    maxLength: 64,
    pattern: '^[a-z][a-z0-9_.-]{0,63}$',
  },
  HostOrTechnicalId: {
    type: 'string',
    minLength: 1,
    maxLength: 64,
    pattern: '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$',
  },
  SemVer: { type: 'string', minLength: 5, maxLength: 64, pattern: SEMVER_PATTERN },
  Sha256Hex: { type: 'string', minLength: 64, maxLength: 64, pattern: '^[0-9a-f]{64}$' },
  KeyId: {
    type: 'string',
    minLength: 1,
    maxLength: 64,
    pattern: '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$',
  },
  NodeClock: {
    type: 'object',
    additionalProperties: false,
    required: ['synchronized', 'offset_ms', 'source'],
    properties: {
      synchronized: { type: 'boolean' },
      offset_ms: { type: 'integer', minimum: -9007199254740991, maximum: 9007199254740991 },
      last_sync_at: { $ref: '#/$defs/Timestamp' },
      source: { $ref: '#/$defs/HostOrTechnicalId' },
    },
  },
  NodeTime: {
    type: 'object',
    additionalProperties: false,
    required: ['started_at', 'ended_at', 'clock'],
    properties: {
      started_at: { $ref: '#/$defs/Timestamp' },
      ended_at: { $ref: '#/$defs/Timestamp' },
      clock: { $ref: '#/$defs/NodeClock' },
    },
  },
  SignalReading: {
    type: 'object',
    additionalProperties: false,
    required: ['signal_id', 'role', 'value', 'valid', 'read_at'],
    properties: {
      signal_id: { $ref: '#/$defs/UUID' },
      role: { $ref: 'enumerations.schema.json#/$defs/signal_role' },
      value: { $ref: 'enumerations.schema.json#/$defs/signal_value' },
      valid: { type: 'boolean' },
      read_at: { $ref: '#/$defs/Timestamp' },
    },
    if: { required: ['valid'], properties: { valid: { const: false } } },
    then: { properties: { value: { const: 'unknown' } } },
  },
  StandardReference: {
    type: 'object',
    additionalProperties: false,
    required: ['standard_id', 'version'],
    properties: {
      standard_id: { $ref: '#/$defs/UUID' },
      version: { type: 'integer', minimum: 1, maximum: 2147483647 },
    },
  },
  EpisodeSegment: {
    type: 'object',
    additionalProperties: false,
    required: ['episode_id', 'segment_index', 'continues', 'max_segment_ms'],
    properties: {
      episode_id: { $ref: '#/$defs/UUIDv7' },
      segment_index: { type: 'integer', minimum: 0, maximum: 2147483647 },
      continues: { type: 'boolean' },
      previous_segment_record_id: { $ref: '#/$defs/UUIDv7' },
      max_segment_ms: { type: 'integer', minimum: 60000, maximum: 3600000 },
    },
    if: { required: ['segment_index'], properties: { segment_index: { const: 0 } } },
    then: { not: { required: ['previous_segment_record_id'] } },
    else: { required: ['previous_segment_record_id'] },
  },
};

const COMPILED_CONSTANT = /^const schema\d+ = (\{.*\});$/;

let constantsCache: readonly unknown[] | undefined;

/** Las constantes JSON de `dist/validators/compiled.js` del paquete instalado. */
export function compiledSchemaConstants(): readonly unknown[] {
  if (constantsCache !== undefined) return constantsCache;
  const require = createRequire(import.meta.url);
  const root = dirname(require.resolve('@vigia/contracts/package.json'));
  const source = readFileSync(join(root, 'dist', 'validators', 'compiled.js'), 'utf8');
  const constants: unknown[] = [];
  for (const line of source.split('\n')) {
    const match = COMPILED_CONSTANT.exec(line);
    if (match?.[1] !== undefined) constants.push(JSON.parse(match[1]));
  }
  if (constants.length === 0) {
    throw new Error('@vigia/contracts: compiled.js sin constantes de esquema; ¿cambió el formato?');
  }
  constantsCache = constants;
  return constants;
}

let registryCache: Readonly<Record<string, unknown>> | undefined;

/** Documentos del contrato por `$id`, para `fromSchema({ registry })`. */
export function contractRegistry(): Readonly<Record<string, unknown>> {
  if (registryCache !== undefined) return registryCache;
  const registry: Record<string, unknown> = {};
  for (const constant of compiledSchemaConstants()) {
    const id = (constant as { $id?: unknown }).$id;
    if (typeof id === 'string' && id.startsWith(CONTRACT_SCHEMAS_BASE)) registry[id] = constant;
  }
  registry[`${CONTRACT_SCHEMAS_BASE}common.schema.json`] = {
    $id: `${CONTRACT_SCHEMAS_BASE}common.schema.json`,
    $defs: COMMON_DEFS,
  };
  registry[`${CONTRACT_SCHEMAS_BASE}enumerations.schema.json`] = {
    $id: `${CONTRACT_SCHEMAS_BASE}enumerations.schema.json`,
    $defs: Object.fromEntries(
      Object.entries(labelsEs).map(([list, values]) => [
        list,
        { type: 'string', enum: Object.keys(values) },
      ]),
    ),
  };
  registryCache = registry;
  return registry;
}

/** El documento raíz `<name>.schema.json` del contrato. */
export function contractSchema(name: string): unknown {
  const uri = `${CONTRACT_SCHEMAS_BASE}${name}.schema.json`;
  const schema = contractRegistry()[uri];
  if (schema === undefined) throw new Error(`@vigia/contracts no trae ${name}.schema.json.`);
  return schema;
}
