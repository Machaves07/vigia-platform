/**
 * Arbitrario de fast-check desde un esquema JSON 2020-12 / OpenAPI 3.1 (LC-APP-24 y 25; generador
 * propio, nota de `tech-stack-decisions.md` §5: no `msw-auto-mock`).
 *
 * Cada esquema se normaliza en una lista de **alternativas** sin composición: se resuelven los
 * `$ref` (también entre documentos, por `$id`), se funden los `allOf`, se reparten los `anyOf` y
 * `oneOf` (con las palabras clave hermanas), cada `if`/`then`/`else` se parte en la rama que cumple
 * el `if` y la que lo niega, y `not` se aplica por construcción (`const`, `enum`, `required`,
 * `contains` y guardas de objeto). Las alternativas imposibles (dos `const` distintos, una
 * propiedad obligatoria sin valor posible) se podan al construir, así que todo lo generado es
 * válido por construcción y nada se descarta al generar salvo los filtros de `pattern`, `format`
 * y longitud de las cadenas.
 *
 * Cualquier palabra clave fuera de la lista soportada detiene la construcción con
 * `UnsupportedSchemaError` y la ruta del esquema: nunca se ignora en silencio. `oneOf` se trata
 * como `anyOf` (no se comprueba la exclusividad); las propiedades del oráculo con Ajv lo cubren.
 *
 * Solo depende de lo dibujado: ni hora del sistema ni estado (AGENTS.md).
 */
import { fullFormats } from 'ajv-formats/dist/formats.js';
import fc from 'fast-check';

type SchemaObject = Readonly<Record<string, unknown>>;
export type JsonSchema = boolean | SchemaObject;

export interface FromSchemaOptions {
  /** Documento que contiene el esquema, para resolver `#/…` (por omisión, el propio esquema). */
  readonly document?: unknown;
  /** URI base del documento (por omisión, su `$id`). */
  readonly uri?: string;
  /** Otros documentos para los `$ref` entre archivos, por URI absoluta (su `$id`). */
  readonly registry?: Readonly<Record<string, unknown>>;
  /** Elementos de más sobre `minItems` en listas sin `maxItems` (por omisión, 3). */
  readonly extraItems?: number;
}

/** El esquema usa una palabra clave o una forma que el generador no sabe satisfacer. */
export class UnsupportedSchemaError extends Error {
  constructor(message: string, where: string) {
    super(`${message} (en ${where})`);
    this.name = 'UnsupportedSchemaError';
  }
}

/** Ningún documento cumple el esquema. */
export class UnsatisfiableSchemaError extends Error {
  constructor(where: string) {
    super(`el esquema no admite ningún valor (en ${where})`);
    this.name = 'UnsatisfiableSchemaError';
  }
}

/** Arbitrario de los documentos que cumplen `schema`. */
export function fromSchema(
  schema: JsonSchema,
  options: FromSchemaOptions = {},
): fc.Arbitrary<unknown> {
  return schemaArbitraries({ ...options, document: options.document ?? schema })(schema);
}

/**
 * Constructor de arbitrarios para varios esquemas del mismo `document`: comparte las cachés, así
 * que cada definición referenciada se construye una sola vez para todos.
 */
export function schemaArbitraries(
  options: FromSchemaOptions,
): (schema: JsonSchema) => fc.Arbitrary<unknown> {
  const document = options.document;
  const id = isObject(document) && typeof document.$id === 'string' ? document.$id : undefined;
  const builder = new Builder(options);
  const context: Context = { document, uri: options.uri ?? id ?? DEFAULT_URI };
  return (schema) => builder.arbitrary([{ schema, context, path: '#' }]);
}

/** Clave canónica de un valor JSON (objetos con las claves ordenadas): igualdad de Ajv. */
export function canonicalKey(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalKey).join(',')}]`;
  if (isObject(value)) {
    const keys = Object.keys(value).sort();
    return `{${keys.map((key) => `${JSON.stringify(key)}:${canonicalKey(value[key])}`).join(',')}}`;
  }
  // `JSON.stringify(undefined)` devuelve `undefined` aunque el tipo diga `string`.
  return value === undefined ? 'undefined' : JSON.stringify(value);
}

// --- Representación interna -----------------------------------------------------------------

const DEFAULT_URI = 'urn:vigia:schema';
const MAX_REF_DEPTH = 32;
const MAX_ALTERNATIVES = 1024;
const MIN_DATE = new Date('2000-01-01T00:00:00.000Z');
const MAX_DATE = new Date('2099-12-31T23:59:59.999Z');

interface Context {
  readonly document: unknown;
  readonly uri: string;
}

interface Located {
  readonly schema: unknown;
  readonly context: Context;
  readonly path: string;
}

type JsonType = 'null' | 'boolean' | 'integer' | 'number' | 'string' | 'array' | 'object';
const ALL_TYPES: readonly JsonType[] = [
  'null',
  'boolean',
  'integer',
  'number',
  'string',
  'array',
  'object',
];

interface Bound {
  readonly value: number;
  readonly exclusive: boolean;
}

interface Contains {
  readonly schema: Located;
  readonly min: number;
  readonly max: number;
}

/** Restricciones de una alternativa, sin composición. */
interface Plain {
  readonly where: string;
  readonly types: ReadonlySet<JsonType> | null;
  readonly values: readonly unknown[] | null;
  readonly excluded: readonly unknown[];
  readonly minLength: number;
  readonly maxLength: number;
  readonly patterns: readonly string[];
  readonly formats: readonly string[];
  readonly lower: Bound | null;
  readonly upper: Bound | null;
  readonly items: readonly Located[];
  readonly minItems: number;
  readonly maxItems: number;
  readonly uniqueItems: boolean;
  readonly contains: readonly Contains[];
  readonly properties: ReadonlyMap<string, readonly Located[]>;
  readonly required: ReadonlySet<string>;
  readonly forbidden: ReadonlySet<string>;
  readonly allowedKeys: ReadonlySet<string> | null;
  readonly additional: readonly Located[] | null;
  readonly propertyNames: readonly Located[];
}

const EMPTY: Plain = {
  where: '#',
  types: null,
  values: null,
  excluded: [],
  minLength: 0,
  maxLength: Infinity,
  patterns: [],
  formats: [],
  lower: null,
  upper: null,
  items: [],
  minItems: 0,
  maxItems: Infinity,
  uniqueItems: false,
  contains: [],
  properties: new Map(),
  required: new Set(),
  forbidden: new Set(),
  allowedKeys: null,
  additional: null,
  propertyNames: [],
};

/** Anotaciones: no cambian qué valida el esquema. */
const ANNOTATIONS = new Set([
  '$schema',
  '$id',
  '$comment',
  '$defs',
  'definitions',
  'title',
  'description',
  'examples',
  'example',
  'default',
  'deprecated',
  'readOnly',
  'writeOnly',
  'discriminator',
  'contentMediaType',
  'contentEncoding',
  'externalDocs',
  'xml',
]);

const SUPPORTED = new Set([
  'type',
  'enum',
  'const',
  '$ref',
  'allOf',
  'anyOf',
  'oneOf',
  'not',
  'if',
  'then',
  'else',
  'minLength',
  'maxLength',
  'pattern',
  'format',
  'minimum',
  'maximum',
  'exclusiveMinimum',
  'exclusiveMaximum',
  'items',
  'minItems',
  'maxItems',
  'uniqueItems',
  'contains',
  'minContains',
  'maxContains',
  'properties',
  'required',
  'additionalProperties',
  'propertyNames',
]);

function isObject(value: unknown): value is Readonly<Record<string, unknown>> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function deepEqual(a: unknown, b: unknown): boolean {
  return canonicalKey(a) === canonicalKey(b);
}

function typeOf(value: unknown): JsonType {
  if (value === null) return 'null';
  if (Array.isArray(value)) return 'array';
  if (typeof value === 'number') return Number.isInteger(value) ? 'integer' : 'number';
  if (typeof value === 'string') return 'string';
  if (typeof value === 'boolean') return 'boolean';
  if (typeof value === 'object') return 'object';
  throw new TypeError(`valor no JSON: ${typeof value}`);
}

function typeAllows(types: ReadonlySet<JsonType> | null, value: unknown): boolean {
  if (types === null) return true;
  const type = typeOf(value);
  return types.has(type) || (type === 'integer' && types.has('number'));
}

/** Longitud en puntos de código, como la cuenta Ajv (`ucs2length`) para `minLength`/`maxLength`. */
function codePoints(text: string): number {
  let length = 0;
  for (let index = 0; index < text.length; index += 1) {
    const code = text.charCodeAt(index);
    if (code >= 0xd800 && code <= 0xdbff && index + 1 < text.length) {
      const next = text.charCodeAt(index + 1);
      if (next >= 0xdc00 && next <= 0xdfff) index += 1;
    }
    length += 1;
  }
  return length;
}

function checkFormat(name: string, value: string): boolean {
  const format: unknown = (fullFormats as Readonly<Record<string, unknown>>)[name];
  if (format === true) return true;
  if (format instanceof RegExp) return format.test(value);
  if (typeof format === 'function') return Boolean((format as (text: string) => unknown)(value));
  if (isObject(format) && typeof format.validate === 'function') {
    return Boolean((format.validate as (text: string) => unknown)(value));
  }
  if (isObject(format) && format.validate instanceof RegExp) return format.validate.test(value);
  return false;
}

function decodePointer(token: string): string {
  return decodeURIComponent(token).replaceAll('~1', '/').replaceAll('~0', '~');
}

// --- Construcción ---------------------------------------------------------------------------

class Builder {
  private readonly registry = new Map<string, unknown>();
  private readonly extraItems: number;
  private readonly ids = new WeakMap<object, number>();
  private readonly alternativesCache = new Map<string, readonly Plain[]>();
  private readonly arbitraryCache = new Map<string, fc.Arbitrary<unknown>>();
  private nextId = 1;
  private refDepth = 0;

  constructor(options: FromSchemaOptions) {
    this.extraItems = options.extraItems ?? 3;
    for (const [uri, document] of Object.entries(options.registry ?? {})) {
      this.registry.set(uri, document);
    }
  }

  /** Arbitrario de los valores que cumplen a la vez todos los esquemas de `list`. */
  arbitrary(list: readonly Located[]): fc.Arbitrary<unknown> {
    const key = this.listKey(list);
    const cached = this.arbitraryCache.get(key);
    if (cached !== undefined) return cached;
    // Un esquema recursivo vuelve a pedir su propia lista mientras se construye: se resuelve
    // al generar, con la profundidad acotada por las propiedades opcionales.
    if (this.building.has(key)) return fc.constant(null).chain(() => this.arbitrary(list));
    this.building.add(key);
    try {
      const alternatives = this.alternativesOfList(list);
      if (alternatives.length === 0) {
        throw new UnsatisfiableSchemaError(list.map((item) => item.path).join(' ∧ ') || '#');
      }
      const arbitraries = alternatives.map((plain) => this.gen(plain));
      const [first] = arbitraries;
      const result =
        arbitraries.length === 1 && first !== undefined ? first : fc.oneof(...arbitraries);
      this.arbitraryCache.set(key, result);
      return result;
    } finally {
      this.building.delete(key);
    }
  }

  private readonly building = new Set<string>();

  private listKey(list: readonly Located[]): string {
    return list
      .map((item) => {
        const schema = item.schema;
        if (typeof schema !== 'object' || schema === null)
          return `${String(schema)}@${item.context.uri}`;
        let id = this.ids.get(schema);
        if (id === undefined) {
          id = this.nextId++;
          this.ids.set(schema, id);
        }
        return `${String(id)}@${item.context.uri}`;
      })
      .join('&');
  }

  // --- Alternativas ---------------------------------------------------------------------------

  private alternativesOfList(list: readonly Located[]): readonly Plain[] {
    if (list.length === 0) return [EMPTY];
    const key = this.listKey(list);
    const cached = this.alternativesCache.get(key);
    if (cached !== undefined) return cached;
    const result = this.product(list.map((item) => this.alternatives(item)));
    this.alternativesCache.set(key, result);
    return result;
  }

  private alternatives(located: Located): readonly Plain[] {
    const { schema, context, path } = located;
    if (schema === true) return [EMPTY];
    if (schema === false) return [];
    if (!isObject(schema)) throw new UnsupportedSchemaError('el esquema no es un objeto', path);
    for (const keyword of Object.keys(schema)) {
      if (!SUPPORTED.has(keyword) && !ANNOTATIONS.has(keyword) && !keyword.startsWith('x-')) {
        throw new UnsupportedSchemaError(`palabra clave «${keyword}» no soportada`, path);
      }
    }
    const lists: (readonly Plain[])[] = [this.own(located)];
    if (schema.$ref !== undefined) {
      if (typeof schema.$ref !== 'string')
        throw new UnsupportedSchemaError('$ref no es texto', path);
      if (this.refDepth >= MAX_REF_DEPTH) {
        throw new UnsupportedSchemaError(
          `$ref recursivo sin objeto intermedio: ${schema.$ref}`,
          path,
        );
      }
      this.refDepth += 1;
      try {
        lists.push(this.alternativesOfList([this.resolve(schema.$ref, context, path)]));
      } finally {
        this.refDepth -= 1;
      }
    }
    for (const [index, sub] of this.schemaList(schema, 'allOf', located).entries()) {
      lists.push(this.alternatives({ ...sub, path: `${path}/allOf/${String(index)}` }));
    }
    for (const keyword of ['anyOf', 'oneOf'] as const) {
      const branches = this.schemaList(schema, keyword, located);
      if (branches.length > 0) {
        lists.push(branches.flatMap((branch) => this.alternatives(branch)));
      }
    }
    if (schema.if !== undefined) {
      const guard = this.child(located, 'if');
      const branch = (keyword: 'then' | 'else'): readonly Plain[] =>
        schema[keyword] === undefined ? [EMPTY] : this.alternatives(this.child(located, keyword));
      lists.push([
        ...this.product([this.alternatives(guard), branch('then')]),
        ...this.product([this.negate(guard), branch('else')]),
      ]);
    }
    return this.product(lists);
  }

  /** Las palabras clave sin composición de `located` (incluido `not`). */
  private own(located: Located): readonly Plain[] {
    const schema = located.schema as SchemaObject;
    const path = located.path;
    let plain: Plain = { ...EMPTY, where: path };
    const number = (keyword: string): number | undefined => {
      const value = schema[keyword];
      if (value === undefined) return undefined;
      if (typeof value !== 'number' || !Number.isFinite(value)) {
        throw new UnsupportedSchemaError(`«${keyword}» no es un número`, path);
      }
      return value;
    };
    if (schema.type !== undefined) {
      const list: unknown[] = Array.isArray(schema.type) ? schema.type : [schema.type];
      for (const type of list) {
        if (!ALL_TYPES.includes(type as JsonType)) {
          throw new UnsupportedSchemaError(`tipo «${String(type)}» desconocido`, path);
        }
      }
      plain = { ...plain, types: new Set(list as JsonType[]) };
    }
    if (schema.const !== undefined) plain = { ...plain, values: [schema.const] };
    if (schema.enum !== undefined) {
      const options: unknown = schema.enum;
      if (!Array.isArray(options)) throw new UnsupportedSchemaError('enum no es lista', path);
      const values = plain.values;
      plain = {
        ...plain,
        values:
          values === null ? options : values.filter((v) => options.some((e) => deepEqual(e, v))),
      };
    }
    plain = {
      ...plain,
      minLength: number('minLength') ?? 0,
      maxLength: number('maxLength') ?? Infinity,
      minItems: number('minItems') ?? 0,
      maxItems: number('maxItems') ?? Infinity,
      uniqueItems: schema.uniqueItems === true,
    };
    if (typeof schema.pattern === 'string') plain = { ...plain, patterns: [schema.pattern] };
    if (typeof schema.format === 'string') plain = { ...plain, formats: [schema.format] };
    const minimum = number('minimum');
    const exclusiveMinimum = number('exclusiveMinimum');
    const maximum = number('maximum');
    const exclusiveMaximum = number('exclusiveMaximum');
    plain = {
      ...plain,
      lower: tighter(
        minimum === undefined ? null : { value: minimum, exclusive: false },
        exclusiveMinimum === undefined ? null : { value: exclusiveMinimum, exclusive: true },
        1,
      ),
      upper: tighter(
        maximum === undefined ? null : { value: maximum, exclusive: false },
        exclusiveMaximum === undefined ? null : { value: exclusiveMaximum, exclusive: true },
        -1,
      ),
    };
    if (schema.items !== undefined) plain = { ...plain, items: [this.child(located, 'items')] };
    if (schema.contains !== undefined) {
      plain = {
        ...plain,
        contains: [
          {
            schema: this.child(located, 'contains'),
            min: number('minContains') ?? 1,
            max: number('maxContains') ?? Infinity,
          },
        ],
      };
    }
    if (schema.properties !== undefined) {
      if (!isObject(schema.properties)) throw new UnsupportedSchemaError('properties', path);
      const properties = new Map<string, readonly Located[]>();
      for (const name of Object.keys(schema.properties)) {
        properties.set(name, [
          {
            schema: schema.properties[name],
            context: located.context,
            path: `${path}/properties/${name}`,
          },
        ]);
      }
      plain = { ...plain, properties };
    }
    if (schema.required !== undefined) {
      if (!Array.isArray(schema.required) || !schema.required.every((k) => typeof k === 'string')) {
        throw new UnsupportedSchemaError('required no es una lista de textos', path);
      }
      plain = { ...plain, required: new Set(schema.required) };
    }
    if (schema.additionalProperties === false) {
      plain = { ...plain, allowedKeys: new Set(plain.properties.keys()) };
    } else if (isObject(schema.additionalProperties)) {
      plain = { ...plain, additional: [this.child(located, 'additionalProperties')] };
    }
    if (schema.propertyNames !== undefined) {
      plain = { ...plain, propertyNames: [this.child(located, 'propertyNames')] };
    }
    const normalized = this.normalize(plain);
    const own = normalized === null ? [] : [normalized];
    if (schema.not === undefined) return own;
    return this.product([own, this.negate(this.child(located, 'not'))]);
  }

  /** Alternativas cuya unión es el complemento de `located` (solo formas construibles). */
  private negate(located: Located): readonly Plain[] {
    const { schema, path } = located;
    if (schema === true) return [];
    if (schema === false) return [EMPTY];
    if (!isObject(schema)) throw new UnsupportedSchemaError('not de algo que no es esquema', path);
    const keywords = Object.keys(schema).filter((k) => !ANNOTATIONS.has(k));
    const only = (...allowed: string[]): boolean => keywords.every((k) => allowed.includes(k));
    const base = { ...EMPTY, where: path };
    if (keywords.length === 1 && schema.$ref !== undefined) {
      if (typeof schema.$ref !== 'string')
        throw new UnsupportedSchemaError('$ref no es texto', path);
      return this.negate(this.resolve(schema.$ref, located.context, path));
    }
    if (only('const')) return [{ ...base, excluded: [schema.const] }];
    if (only('enum') && Array.isArray(schema.enum)) return [{ ...base, excluded: schema.enum }];
    if (only('contains')) {
      // Ningún elemento cumple `contains`: cada elemento cumple su negación.
      const inner = this.child(located, 'contains');
      return [
        { ...base, items: [{ ...inner, schema: { not: inner.schema }, path: `${path}/not` }] },
      ];
    }
    if (
      only('type', 'required', 'properties') &&
      (schema.type === undefined || schema.type === 'object')
    ) {
      // Guarda de objeto: falta una clave exigida, o una propiedad presente no cumple la suya.
      const required = Array.isArray(schema.required) ? (schema.required as string[]) : [];
      const properties = isObject(schema.properties) ? schema.properties : {};
      const result: Plain[] = required.map((name) => ({ ...base, forbidden: new Set([name]) }));
      for (const name of Object.keys(properties)) {
        const property = this.child(this.child(located, 'properties'), name);
        result.push({
          ...base,
          required: new Set([name]),
          properties: new Map([[name, [{ ...property, schema: { not: property.schema } }]]]),
        });
      }
      return result.map((plain) => this.normalize(plain)).filter((p): p is Plain => p !== null);
    }
    throw new UnsupportedSchemaError(
      `no se sabe negar ${JSON.stringify(schema).slice(0, 80)}`,
      path,
    );
  }

  private child(located: Located, ...keys: string[]): Located {
    let schema: unknown = located.schema;
    for (const key of keys) schema = isObject(schema) ? schema[key] : undefined;
    return { schema, context: located.context, path: `${located.path}/${keys.join('/')}` };
  }

  private schemaList(schema: SchemaObject, keyword: string, located: Located): Located[] {
    const value = schema[keyword];
    if (value === undefined) return [];
    if (!Array.isArray(value) || value.length === 0) {
      throw new UnsupportedSchemaError(`«${keyword}» no es una lista no vacía`, located.path);
    }
    return value.map((sub: unknown, index) => ({
      schema: sub,
      context: located.context,
      path: `${located.path}/${keyword}/${String(index)}`,
    }));
  }

  private resolve(reference: string, context: Context, path: string): Located {
    const hash = reference.indexOf('#');
    const address = hash === -1 ? reference : reference.slice(0, hash);
    const fragment = hash === -1 ? '' : reference.slice(hash + 1);
    let target = context;
    if (address !== '') {
      let uri: string;
      try {
        uri = new URL(address, context.uri).href;
      } catch {
        throw new UnsupportedSchemaError(`$ref «${reference}» sin URI base`, path);
      }
      if (!this.registry.has(uri)) {
        throw new UnsupportedSchemaError(`$ref a un documento no registrado: ${uri}`, path);
      }
      target = { document: this.registry.get(uri), uri };
    }
    let schema: unknown = target.document;
    if (fragment !== '') {
      if (!fragment.startsWith('/')) {
        throw new UnsupportedSchemaError(`$ref con ancla no soportado: ${reference}`, path);
      }
      for (const token of fragment.slice(1).split('/').map(decodePointer)) {
        if (Array.isArray(schema) && /^\d+$/.test(token)) schema = schema[Number(token)];
        else if (isObject(schema) && Object.hasOwn(schema, token)) schema = schema[token];
        else throw new UnsupportedSchemaError(`$ref sin destino: ${reference}`, path);
      }
    }
    return { schema, context: target, path: `${target.uri}#${fragment}` };
  }

  // --- Fusión ---------------------------------------------------------------------------------

  private product(lists: readonly (readonly Plain[])[]): readonly Plain[] {
    let result: readonly Plain[] = [EMPTY];
    for (const list of lists) {
      const next: Plain[] = [];
      for (const left of result) {
        for (const right of list) {
          const merged = left === EMPTY ? right : right === EMPTY ? left : this.merge(left, right);
          if (merged !== null) next.push(merged);
        }
      }
      if (next.length > MAX_ALTERNATIVES) {
        throw new UnsupportedSchemaError('demasiadas alternativas', list[0]?.where ?? '#');
      }
      result = next;
    }
    return result;
  }

  private merge(a: Plain, b: Plain): Plain | null {
    let types: ReadonlySet<JsonType> | null = a.types ?? b.types;
    if (a.types !== null && b.types !== null) {
      const left = a.types;
      const right = b.types;
      types = new Set(
        ALL_TYPES.filter(
          (type) =>
            (left.has(type) && right.has(type)) ||
            (type === 'integer' &&
              ((left.has('integer') && right.has('number')) ||
                (left.has('number') && right.has('integer')))),
        ),
      );
    }
    let values = a.values ?? b.values;
    if (a.values !== null && b.values !== null) {
      const right = b.values;
      values = a.values.filter((value) => right.some((other) => deepEqual(value, other)));
    }
    if (a.additional !== null && [...b.properties.keys()].some((k) => !a.properties.has(k))) {
      throw new UnsupportedSchemaError(
        'additionalProperties con esquema al fundir propiedades',
        b.where,
      );
    }
    if (b.additional !== null && [...a.properties.keys()].some((k) => !b.properties.has(k))) {
      throw new UnsupportedSchemaError(
        'additionalProperties con esquema al fundir propiedades',
        a.where,
      );
    }
    const properties = new Map(a.properties);
    for (const [name, list] of b.properties)
      properties.set(name, [...(properties.get(name) ?? []), ...list]);
    const allowedKeys =
      a.allowedKeys === null
        ? b.allowedKeys
        : b.allowedKeys === null
          ? a.allowedKeys
          : new Set([...a.allowedKeys].filter((k) => b.allowedKeys?.has(k)));
    return this.normalize({
      where: a.where,
      types,
      values,
      excluded: [...a.excluded, ...b.excluded],
      minLength: Math.max(a.minLength, b.minLength),
      maxLength: Math.min(a.maxLength, b.maxLength),
      patterns: [...new Set([...a.patterns, ...b.patterns])],
      formats: [...new Set([...a.formats, ...b.formats])],
      lower: tighter(a.lower, b.lower, 1),
      upper: tighter(a.upper, b.upper, -1),
      items: [...a.items, ...b.items],
      minItems: Math.max(a.minItems, b.minItems),
      maxItems: Math.min(a.maxItems, b.maxItems),
      uniqueItems: a.uniqueItems || b.uniqueItems,
      contains: [...a.contains, ...b.contains],
      properties,
      required: new Set([...a.required, ...b.required]),
      forbidden: new Set([...a.forbidden, ...b.forbidden]),
      allowedKeys,
      additional:
        a.additional === null
          ? b.additional
          : b.additional === null
            ? a.additional
            : [...a.additional, ...b.additional],
      propertyNames: [...a.propertyNames, ...b.propertyNames],
    });
  }

  /** Simplifica una alternativa y descarta (`null`) la que no admite ningún valor. */
  private normalize(input: Plain): Plain | null {
    let plain = input;
    const drop = (kinds: readonly JsonType[]): void => {
      const types = new Set(plain.types ?? ALL_TYPES);
      for (const kind of kinds) types.delete(kind);
      plain = { ...plain, types };
    };
    if (plain.minLength > plain.maxLength) drop(['string']);
    if (plain.lower !== null && plain.upper !== null) {
      const { lower, upper } = plain;
      if (
        lower.value > upper.value ||
        (lower.value === upper.value && (lower.exclusive || upper.exclusive))
      ) {
        drop(['integer', 'number']);
      }
      const [min, max] = integerRange(plain);
      if (min > max) drop(['integer']);
    }
    // Una propiedad cuyo esquema fundido no admite valores no puede aparecer.
    const forbidden = new Set(plain.forbidden);
    for (const [name, list] of plain.properties) {
      if (list.length > 1 && !forbidden.has(name) && this.alternativesOfList(list).length === 0) {
        forbidden.add(name);
      }
    }
    plain = { ...plain, forbidden };
    if (
      [...plain.required].some(
        (k) => plain.forbidden.has(k) || (plain.allowedKeys !== null && !plain.allowedKeys.has(k)),
      )
    ) {
      drop(['object']);
    }
    let maxItems = plain.maxItems;
    if (plain.items.length > 1 && this.alternativesOfList(plain.items).length === 0) maxItems = 0;
    for (const contains of plain.contains) {
      if (
        contains.min > 0 &&
        this.alternativesOfList([...plain.items, contains.schema]).length === 0
      ) {
        maxItems = -1;
      }
      if (contains.min > contains.max) maxItems = -1;
    }
    plain = { ...plain, maxItems };
    if (plain.minItems > plain.maxItems) drop(['array']);
    if (plain.types !== null && plain.types.size === 0) return null;
    if (
      plain.types !== null &&
      [...plain.types].every((t) => t === 'boolean') &&
      plain.values === null &&
      plain.excluded.length > 0
    ) {
      plain = { ...plain, values: [true, false] };
    }
    if (plain.values !== null) {
      const { types, excluded } = plain;
      const values = plain.values.filter(
        (value) => typeAllows(types, value) && !excluded.some((other) => deepEqual(value, other)),
      );
      if (values.length === 0) return null;
      plain = { ...plain, values };
    }
    return plain;
  }

  // --- Generación -----------------------------------------------------------------------------

  private gen(plain: Plain): fc.Arbitrary<unknown> {
    if (plain.values !== null) return fc.constantFrom(...plain.values);
    const types = plain.types === null ? inferTypes(plain) : [...plain.types];
    const arbitraries = types.map((type) => this.genType(type, plain));
    const [first] = arbitraries;
    const arbitrary =
      arbitraries.length === 1 && first !== undefined ? first : fc.oneof(...arbitraries);
    if (plain.excluded.length === 0) return arbitrary;
    const excluded = plain.excluded;
    return arbitrary.filter((value) => !excluded.some((other) => deepEqual(value, other)));
  }

  private genType(type: JsonType | 'any', plain: Plain): fc.Arbitrary<unknown> {
    switch (type) {
      case 'null':
        return fc.constant(null);
      case 'boolean':
        return fc.boolean();
      case 'integer':
        return integerArbitrary(plain);
      case 'number':
        return numberArbitrary(plain);
      case 'string':
        return stringArbitrary(plain);
      case 'array':
        return this.arrayArbitrary(plain);
      case 'object':
        return this.objectArbitrary(plain);
      case 'any':
        return fc
          .jsonValue({ maxDepth: 2 })
          .map((value) => JSON.parse(JSON.stringify(value)) as unknown);
    }
  }

  private arrayArbitrary(plain: Plain): fc.Arbitrary<unknown[]> {
    const item = this.arbitrary(plain.items);
    const minLength = plain.minItems;
    const maxLength = Math.min(plain.maxItems, minLength + this.extraItems);
    if (plain.contains.length === 0) {
      return plain.uniqueItems
        ? fc.uniqueArray(item, { minLength, maxLength, selector: canonicalKey })
        : fc.array(item, { minLength, maxLength });
    }
    if (plain.uniqueItems)
      throw new UnsupportedSchemaError('uniqueItems con contains', plain.where);
    const bounded = plain.contains.filter((c) => c.max !== Infinity);
    if (bounded.length > 0 && plain.contains.length > 1) {
      throw new UnsupportedSchemaError('varios contains con maxContains', plain.where);
    }
    const designated = plain.contains.map((c) =>
      fc.array(this.arbitrary([...plain.items, c.schema]), {
        minLength: c.min,
        maxLength: Math.min(c.max, c.min + 1),
      }),
    );
    const rest = this.arbitrary([
      ...plain.items,
      ...bounded.map((c) => ({
        ...c.schema,
        schema: { not: c.schema.schema },
        path: `${c.schema.path}/not`,
      })),
    ]);
    return fc
      .tuple(...designated)
      .filter((groups) => groups.flat().length <= plain.maxItems)
      .chain((groups) => {
        const fixed = groups.flat();
        return fc
          .array(rest, {
            minLength: Math.max(0, minLength - fixed.length),
            maxLength: Math.max(
              0,
              Math.min(plain.maxItems, fixed.length + this.extraItems) - fixed.length,
            ),
          })
          .map((others) => [...fixed, ...others]);
      });
  }

  private objectArbitrary(plain: Plain): fc.Arbitrary<Record<string, unknown>> {
    const declared = [...new Set([...plain.properties.keys(), ...plain.required])];
    const keys = declared.filter(
      (key) =>
        !plain.forbidden.has(key) && (plain.allowedKeys === null || plain.allowedKeys.has(key)),
    );
    const model: Record<string, fc.Arbitrary<unknown>> = {};
    for (const key of keys) {
      Object.defineProperty(model, key, {
        value: this.arbitrary(plain.properties.get(key) ?? []),
        enumerable: true,
      });
    }
    const record = fc.record(model, {
      requiredKeys: keys.filter((key) => plain.required.has(key)),
    });
    if (plain.allowedKeys !== null) return record;
    // Objeto abierto: también claves no declaradas (las de `additionalProperties` con esquema, o
    // cualquier valor JSON si no lo restringe), como las que añade una versión más nueva.
    const known = new Set(declared);
    const names =
      plain.propertyNames.length > 0
        ? (this.arbitrary(plain.propertyNames) as fc.Arbitrary<string>)
        : fc.stringMatching(/^[a-z][a-z0-9_]{0,11}$/);
    const extras = fc.dictionary(
      names.filter(
        (name) =>
          typeof name === 'string' &&
          !known.has(name) &&
          !plain.forbidden.has(name) &&
          name !== '__proto__',
      ),
      plain.additional === null
        ? this.genType('any', { ...EMPTY, where: plain.where })
        : this.arbitrary(plain.additional),
      { maxKeys: plain.additional === null ? 2 : 3 },
    );
    return fc.tuple(record, extras).map(([own, extra]) => ({ ...extra, ...own }));
  }
}

function tighter(a: Bound | null, b: Bound | null, direction: 1 | -1): Bound | null {
  if (a === null) return b;
  if (b === null) return a;
  if (a.value === b.value) return { value: a.value, exclusive: a.exclusive || b.exclusive };
  return (a.value - b.value) * direction > 0 ? a : b;
}

function inferTypes(plain: Plain): (JsonType | 'any')[] {
  if (
    plain.properties.size > 0 ||
    plain.required.size > 0 ||
    plain.allowedKeys !== null ||
    plain.additional !== null
  ) {
    return ['object'];
  }
  if (plain.items.length > 0 || plain.contains.length > 0 || plain.minItems > 0) return ['array'];
  if (
    plain.patterns.length > 0 ||
    plain.formats.length > 0 ||
    plain.minLength > 0 ||
    plain.maxLength !== Infinity
  ) {
    return ['string'];
  }
  if (plain.lower !== null || plain.upper !== null) return ['number'];
  return ['any'];
}

/** Enteros admitidos por los límites; sin uno de los dos, un tramo de 2^31 junto al otro. */
function integerRange(plain: Plain): [number, number] {
  const lower = plain.lower;
  const upper = plain.upper;
  let min =
    lower === null
      ? -(2 ** 31)
      : lower.exclusive
        ? Math.floor(lower.value) + 1
        : Math.ceil(lower.value);
  let max =
    upper === null
      ? 2 ** 31 - 1
      : upper.exclusive
        ? Math.ceil(upper.value) - 1
        : Math.floor(upper.value);
  min = Math.max(min, Number.MIN_SAFE_INTEGER);
  max = Math.min(max, Number.MAX_SAFE_INTEGER);
  if (lower === null && upper !== null) min = Math.min(max, Math.max(min, max - 2 ** 31));
  if (upper === null && lower !== null) max = Math.max(min, Math.min(max, min + 2 ** 31));
  return [min, max];
}

function integerArbitrary(plain: Plain): fc.Arbitrary<number> {
  const [min, max] = integerRange(plain);
  if (min >= -(2 ** 31) && max <= 2 ** 31 - 1) return fc.integer({ min, max });
  return fc.bigInt({ min: BigInt(min), max: BigInt(max) }).map(Number);
}

function numberArbitrary(plain: Plain): fc.Arbitrary<number> {
  const lower = plain.lower;
  const upper = plain.upper;
  return fc
    .double({
      noNaN: true,
      noDefaultInfinity: true,
      ...(lower === null ? { min: -1e12 } : { min: lower.value, minExcluded: lower.exclusive }),
      ...(upper === null ? { max: 1e12 } : { max: upper.value, maxExcluded: upper.exclusive }),
    })
    .map((value) => (Object.is(value, -0) ? 0 : value));
}

function stringArbitrary(plain: Plain): fc.Arbitrary<string> {
  const { minLength, maxLength, patterns, formats } = plain;
  let base: fc.Arbitrary<string>;
  let checkFormats = formats;
  const [pattern, ...otherPatterns] = patterns;
  if (pattern !== undefined) {
    base = fc.stringMatching(new RegExp(`^(?:${pattern})$`));
  } else if (formats.includes('uuid')) {
    base = fc.uuid();
  } else if (formats.includes('date-time')) {
    base = fc
      .date({ min: MIN_DATE, max: MAX_DATE, noInvalidDate: true })
      .map((d) => d.toISOString());
  } else if (formats.includes('date')) {
    base = fc
      .date({ min: MIN_DATE, max: MAX_DATE, noInvalidDate: true })
      .map((d) => d.toISOString().slice(0, 10));
  } else if (formats.includes('email')) {
    base = fc.emailAddress();
  } else {
    base = fc.string({ minLength, maxLength: Math.min(maxLength, minLength + 64) });
    checkFormats = formats.filter((format) => format !== 'binary');
  }
  for (const format of checkFormats) {
    if (!['uuid', 'date-time', 'date', 'email', 'binary'].includes(format)) {
      throw new UnsupportedSchemaError(`format «${format}» no soportado`, plain.where);
    }
  }
  const regexes = otherPatterns.map((other) => new RegExp(other, 'u'));
  return base.filter((text) => {
    const length = codePoints(text);
    return (
      length >= minLength &&
      length <= maxLength &&
      regexes.every((regex) => regex.test(text)) &&
      checkFormats.every((format) => checkFormat(format, text))
    );
  });
}
