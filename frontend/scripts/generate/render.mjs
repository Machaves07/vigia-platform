/**
 * Lo que `npm run generate` escribe desde `backend/openapi/app.yaml` (LC-APP-12 y 16, NFR-APP-48).
 *
 * - `src/client/schema.d.ts`: tipos de `openapi-typescript`.
 * - `src/client/index.ts`: `createApiClient` sobre `openapi-fetch`, sin estado, y `OPERATIONS`.
 * - `openapi/bodies/<operationId>.json`: esquema JSON del cuerpo y de los parámetros de consulta de
 *   cada operación que los tiene, con las mismas longitudes, formatos, patrones y obligatoriedad
 *   que Pydantic publicó, y `additionalProperties: false` en todo objeto (BR-APP-64).
 * - `src/client/validators.js` (y su `.d.ts`): esos esquemas compilados por el Ajv del contrato en
 *   código autónomo. La política de contenido no admite `unsafe-eval`, así que el navegador no
 *   puede compilar con Ajv: ejecuta lo que se compiló aquí, como `@vigia/contracts`.
 *
 * Todo es función del texto de la especificación y de las versiones fijadas en
 * `package-lock.json`: dos corridas dan los mismos bytes.
 */
import Ajv2020 from 'ajv/dist/2020.js';
import standaloneCode from 'ajv/dist/standalone/index.js';
import addFormats from 'ajv-formats';
import openapiTS, { astToString } from 'openapi-typescript';
import { format, resolveConfig } from 'prettier';

import { parseOpenApiYaml } from './openapiYaml.mjs';

/** @typedef {import('./types').JsonValue} JsonValue */
/** @typedef {import('./types').JsonObject} JsonObject */
/** @typedef {{ operationId: string, method: string, path: string, operation: JsonObject }} Operation */

export const SPEC_PATH = '../backend/openapi/app.yaml';
export const BODIES_DIR = 'openapi/bodies';

const NOTICE =
  'Generado por `npm run generate` (scripts/generate.mjs) desde backend/openapi/app.yaml: ' +
  'no se edita a mano; `npm run generate:check` exige diferencia vacía (NFR-APP-48).';
const CODE_HEADER = `// ${NOTICE}\n`;

const METHODS = ['get', 'put', 'post', 'delete', 'options', 'head', 'patch', 'trace'];
const COMPONENT_REF = /^#\/components\/schemas\/([A-Za-z0-9_.-]+)$/;
const IDENTIFIER = /^[A-Za-z_][A-Za-z0-9_]*$/;
const DIALECT = 'https://json-schema.org/draft/2020-12/schema';
/**
 * Identificador relativo de las definiciones compartidas que se compilan: el código autónomo las
 * incrusta y `src/` no lleva URL absolutas (NFR-APP-08). Lo compilado no declara `$schema`: el
 * Ajv de 2020-12 ya usa ese dialecto.
 */
const COMPONENTS_ID = 'components.json';

/** Palabras clave que conservan los esquemas espejo: solo las que deciden un veredicto. */
const KEEP = new Set([
  'type',
  'enum',
  'const',
  'format',
  'pattern',
  'minLength',
  'maxLength',
  'minimum',
  'maximum',
  'exclusiveMinimum',
  'exclusiveMaximum',
  'multipleOf',
  'minItems',
  'maxItems',
  'uniqueItems',
  'required',
  'minProperties',
  'maxProperties',
]);
/** Palabras clave cuyo valor es un esquema, una lista de esquemas o un mapa de esquemas. */
const SUBSCHEMA = new Set(['items', 'additionalProperties', 'propertyNames', 'contains', 'not']);
const SUBSCHEMA_LIST = new Set(['anyOf', 'oneOf', 'allOf', 'prefixItems']);
const SUBSCHEMA_MAP = new Set(['properties', 'patternProperties']);
/**
 * Anotaciones que no cambian el veredicto y se omiten; `discriminator` de OpenAPI solo nombra la
 * rama, que el `oneOf` ya decide.
 */
const DROP = new Set([
  'title',
  'description',
  'default',
  'examples',
  'deprecated',
  'discriminator',
]);

export class GenerateError extends Error {
  /** @param {string} message */
  constructor(message) {
    super(message);
    this.name = 'GenerateError';
  }
}

/**
 * @param {JsonValue | undefined} value
 * @returns {value is JsonObject}
 */
const isObject = (value) => typeof value === 'object' && value !== null && !Array.isArray(value);

/**
 * @param {JsonObject} parent
 * @param {string} key
 * @param {string} where
 * @returns {JsonObject}
 */
function objectAt(parent, key, where) {
  const value = parent[key];
  if (!isObject(value)) throw new GenerateError(`${where}: «${key}» no es un objeto`);
  return value;
}

/**
 * Operaciones de la especificación en su orden, con `operationId` único y apto como identificador.
 *
 * @param {JsonObject} document
 * @returns {Operation[]}
 */
export function listOperations(document) {
  /** @type {Operation[]} */
  const operations = [];
  const seen = new Set();
  for (const [path, item] of Object.entries(objectAt(document, 'paths', 'app.yaml'))) {
    if (!isObject(item)) throw new GenerateError(`app.yaml: la ruta ${path} no es un objeto`);
    for (const [method, operation] of Object.entries(item)) {
      if (!METHODS.includes(method)) {
        throw new GenerateError(`app.yaml: ${path} declara «${method}», que no es un método`);
      }
      if (!isObject(operation))
        throw new GenerateError(`app.yaml: ${method} ${path} no es un objeto`);
      const { operationId } = operation;
      if (typeof operationId !== 'string' || !IDENTIFIER.test(operationId)) {
        throw new GenerateError(`app.yaml: ${method} ${path} sin operationId válido`);
      }
      if (seen.has(operationId))
        throw new GenerateError(`app.yaml: operationId repetido ${operationId}`);
      seen.add(operationId);
      operations.push({ operationId, method, path, operation });
    }
  }
  return operations;
}

/**
 * Nombres de los esquemas de `components` que alcanza `node`, en el orden en que se encuentran.
 *
 * @param {JsonValue} node
 * @param {JsonObject} components
 * @param {Set<string>} found
 * @returns {Set<string>}
 */
function reachable(node, components, found = new Set()) {
  if (Array.isArray(node)) {
    for (const item of node) reachable(item, components, found);
  } else if (isObject(node)) {
    for (const [key, value] of Object.entries(node)) {
      if (key === '$ref' && typeof value === 'string') {
        const name = COMPONENT_REF.exec(value)?.[1];
        if (name === undefined || !isObject(components[name])) {
          throw new GenerateError(`app.yaml: referencia no resuelta ${value}`);
        }
        if (!found.has(name)) {
          found.add(name);
          reachable(components[name], components, found);
        }
      } else {
        reachable(value, components, found);
      }
    }
  }
  return found;
}

/**
 * @param {Operation} operation
 * @returns {JsonObject[]}
 */
function queryParameters({ operation, operationId }) {
  const parameters = operation.parameters ?? [];
  if (!Array.isArray(parameters))
    throw new GenerateError(`${operationId}: parameters no es una lista`);
  return parameters.filter(
    /** @returns {parameter is JsonObject} */ (parameter) =>
      isObject(parameter) && parameter.in === 'query',
  );
}

/**
 * @param {Operation} operation
 * @returns {{ schema: JsonObject, required: boolean } | null}
 */
function jsonBody({ operation, operationId }) {
  const body = operation.requestBody;
  if (body === undefined) return null;
  if (!isObject(body)) throw new GenerateError(`${operationId}: requestBody no es un objeto`);
  const content = objectAt(body, 'content', operationId);
  const types = Object.keys(content);
  if (types.length !== 1 || types[0] !== 'application/json') {
    throw new GenerateError(
      `${operationId}: cuerpo que no es solo application/json (${types.join(', ')})`,
    );
  }
  const media = objectAt(content, 'application/json', operationId);
  return { schema: objectAt(media, 'schema', operationId), required: body.required === true };
}

/**
 * Todo objeto con `properties` de los esquemas que solo usan las peticiones queda cerrado
 * (`additionalProperties: false`): ningún cuerpo de un puerto lleva campos fuera del tipo
 * (BR-APP-64), aunque el servidor acepte alguno para reconocerlo (p. ej. `SampleResultBody`).
 * Un esquema cerrado así que también describa una respuesta detiene la generación: cerrar una
 * respuesta rechazaría datos que el servidor sí envía.
 *
 * @param {JsonObject} document
 * @param {Operation[]} operations
 * @returns {string[]} los esquemas cerrados por la interfaz, para declararlo en cada archivo
 */
function closeRequestObjects(document, operations) {
  const components = objectAt(objectAt(document, 'components', 'app.yaml'), 'schemas', 'app.yaml');
  const requestNodes = operations.flatMap((operation) => [
    jsonBody(operation)?.schema ?? null,
    ...queryParameters(operation),
  ]);
  const requestNames = reachable(requestNodes, components);
  const responseNames = reachable(
    operations.map(({ operation }) => operation.responses ?? {}),
    components,
  );
  /** @type {string[]} */
  const closed = [];
  /** @param {JsonValue} node @param {string} where */
  const close = (node, where) => {
    if (Array.isArray(node)) {
      node.forEach((item) => {
        close(item, where);
      });
      return;
    }
    if (!isObject(node)) return;
    if (isObject(node.properties) && node.additionalProperties !== false) {
      if (isObject(node.additionalProperties)) {
        throw new GenerateError(
          `${where}: objeto con propiedades y additionalProperties con esquema`,
        );
      }
      node.additionalProperties = false;
      if (!closed.includes(where)) closed.push(where);
    }
    for (const [key, value] of Object.entries(node)) {
      if (key === 'properties' && isObject(value)) {
        for (const property of Object.values(value)) close(property, where);
      } else if (key !== 'const' && key !== 'enum' && key !== 'default' && key !== 'examples') {
        close(value, where);
      }
    }
  };
  for (const name of requestNames) {
    const before = closed.length;
    close(components[name] ?? null, name);
    if (closed.length > before && responseNames.has(name)) {
      throw new GenerateError(`${name}: abierto a campos extra y usado también en una respuesta`);
    }
  }
  for (const operation of operations) {
    const body = jsonBody(operation);
    if (body !== null) close(body.schema, operation.operationId);
    for (const parameter of queryParameters(operation))
      close(parameter.schema ?? null, operation.operationId);
  }
  return closed.sort();
}

/**
 * Esquema espejo: solo las palabras clave que deciden el veredicto, referencias a `prefix`.
 *
 * @param {JsonValue} node
 * @param {string} prefix
 * @param {string} where
 * @returns {JsonValue}
 */
function mirror(node, prefix, where) {
  if (typeof node === 'boolean') return node;
  if (!isObject(node)) throw new GenerateError(`${where}: esquema que no es un objeto`);
  /** @type {JsonObject} */
  const result = {};
  for (const [key, value] of Object.entries(node)) {
    if (DROP.has(key)) continue;
    if (key === '$ref') {
      const name = typeof value === 'string' ? COMPONENT_REF.exec(value)?.[1] : undefined;
      if (name === undefined)
        throw new GenerateError(`${where}: referencia no admitida ${String(value)}`);
      result.$ref = `${prefix}${name}`;
    } else if (KEEP.has(key)) {
      result[key] = value;
    } else if (SUBSCHEMA.has(key)) {
      result[key] = mirror(value, prefix, where);
    } else if (SUBSCHEMA_LIST.has(key) && Array.isArray(value)) {
      result[key] = value.map((item) => mirror(item, prefix, where));
    } else if (SUBSCHEMA_MAP.has(key) && isObject(value)) {
      result[key] = Object.fromEntries(
        Object.entries(value).map(([name, item]) => [name, mirror(item, prefix, where)]),
      );
    } else {
      throw new GenerateError(`${where}: palabra clave «${key}» sin traducción al esquema espejo`);
    }
  }
  return result;
}

/**
 * @param {JsonValue} root
 * @param {JsonObject} components
 * @param {string} where
 * @returns {JsonObject}
 */
function selfContained(root, components, where) {
  const names = [...reachable(root, components)].sort();
  const schema = /** @type {JsonObject} */ (mirror(root, '#/$defs/', where));
  return {
    $schema: DIALECT,
    ...schema,
    ...(names.length > 0
      ? {
          $defs: Object.fromEntries(
            names.map((name) => [name, mirror(components[name] ?? null, '#/$defs/', name)]),
          ),
        }
      : {}),
  };
}

/**
 * @param {Operation} operation
 * @returns {JsonObject | null}
 */
function querySchema(operation) {
  const parameters = queryParameters(operation);
  if (parameters.length === 0) return null;
  /** @type {JsonObject} */
  const properties = {};
  /** @type {string[]} */
  const required = [];
  for (const parameter of parameters) {
    const { name } = parameter;
    if (typeof name !== 'string' || Object.hasOwn(properties, name)) {
      throw new GenerateError(
        `${operation.operationId}: parámetro de consulta sin nombre o repetido`,
      );
    }
    properties[name] = parameter.schema ?? {};
    if (parameter.required === true) required.push(name);
  }
  return {
    type: 'object',
    properties,
    ...(required.length > 0 ? { required } : {}),
    additionalProperties: false,
  };
}

/** @param {string} text @param {string} filepath */
async function pretty(text, filepath) {
  const options = (await resolveConfig(filepath)) ?? {};
  return format(text, { ...options, filepath });
}

/**
 * @param {Ajv2020} ajv
 * @param {Record<string, string>} exports nombre exportado → clave del esquema en Ajv
 */
function standalone(ajv, exports) {
  let compiled = standaloneCode(ajv, exports);
  // Como en `@vigia/contracts`: Ajv emite `require(...)` para sus auxiliares aun con `esm: true`;
  // cada uno pasa a una importación estática y `cjs` entrega `module.exports` en Node y en Vite.
  /** @type {Map<string, string>} */
  const imports = new Map();
  compiled = compiled.replace(/require\("([^"]+)"\)/g, (_, /** @type {string} */ specifier) => {
    if (!imports.has(specifier)) imports.set(specifier, `runtime${String(imports.size)}`);
    return imports.get(specifier) ?? '';
  });
  return [
    ...[...imports].map(([specifier, name]) => `import * as ${name}Module from '${specifier}.js';`),
    'const cjs = (module) => (module.default?.__esModule ? module.default : module);',
    ...[...imports.values()].map((name) => `const ${name} = cjs(${name}Module);`),
    compiled.replace(/^"use strict";/, '').trim(),
  ].join('\n');
}

/**
 * Contenido de cada archivo generado, por ruta relativa a `frontend/`.
 *
 * @param {string} source texto de `app.yaml`
 * @returns {Promise<Map<string, string>>}
 */
export async function renderAll(source) {
  const document = parseOpenApiYaml(source);
  const operations = listOperations(document);
  const closed = closeRequestObjects(document, operations);
  const components = objectAt(objectAt(document, 'components', 'app.yaml'), 'schemas', 'app.yaml');
  /** @type {Map<string, string>} */
  const files = new Map();

  // Tipos y cliente.
  const types = astToString(await openapiTS(/** @type {never} */ (document)));
  // openapi-typescript declara las cabeceras de cada respuesta con una firma de índice.
  const typesHeader =
    `${CODE_HEADER}/* eslint-disable @typescript-eslint/consistent-indexed-object-style -- ` +
    'forma que emite openapi-typescript. */\n';
  files.set('src/client/schema.d.ts', await pretty(typesHeader + types, 'src/client/schema.d.ts'));
  const operationEntries = operations
    .map(
      ({ operationId, method, path }) =>
        `  ${operationId}: { method: '${method}', path: '${path}' },`,
    )
    .join('\n');
  const index = `${CODE_HEADER}
/**
 * Cliente de la plataforma (LC-APP-12): tipos de \`openapi-typescript\` y \`openapi-fetch\` sin
 * estado. Solo \`src/ports/**\` lo importa (BR-APP-62); tiempos de espera, cabeceras y errores son
 * de \`ports.base\`.
 */
import createClient, { type Client, type ClientOptions } from 'openapi-fetch';

import type { paths } from './schema';

/** Cliente tipado de las ${String(operations.length)} operaciones de \`app.yaml\`; no guarda estado propio. */
export function createApiClient(options: ClientOptions = {}): Client<paths> {
  return createClient<paths>(options);
}

/** Operaciones de \`app.yaml\`: \`operationId\` → método y ruta, en el orden de la especificación. */
export const OPERATIONS = {
${operationEntries}
} as const satisfies Readonly<Record<string, { readonly method: string; readonly path: keyof paths }>>;
`;
  files.set('src/client/index.ts', await pretty(index, 'src/client/index.ts'));

  // Esquemas espejo, uno por operación con cuerpo o con parámetros de consulta.
  const ajv = new Ajv2020({
    strict: true,
    allErrors: true,
    coerceTypes: false,
    useDefaults: false,
    removeAdditional: false,
    validateFormats: true,
    inlineRefs: false,
    messages: false,
    code: { source: true, esm: true, lines: true, optimize: true },
  });
  addFormats(ajv, ['date-time', 'uuid', 'email']);
  const shared = [
    ...reachable(
      operations.flatMap((operation) => [
        jsonBody(operation)?.schema ?? {},
        querySchema(operation) ?? {},
      ]),
      components,
    ),
  ].sort();
  ajv.addSchema({
    $id: COMPONENTS_ID,
    $defs: Object.fromEntries(
      shared.map((name) => [
        name,
        mirror(components[name] ?? null, `${COMPONENTS_ID}#/$defs/`, name),
      ]),
    ),
  });
  /** @type {Record<string, string>} */
  const exports = {};
  /** @type {string[]} */
  const bodyIds = [];
  /** @type {string[]} */
  const queryIds = [];
  for (const operation of [...operations].sort((a, b) =>
    a.operationId < b.operationId ? -1 : 1,
  )) {
    const { operationId, method, path } = operation;
    const body = jsonBody(operation);
    const query = querySchema(operation);
    if (body === null && query === null) continue;
    const uses = new Set([operationId, ...reachable([body?.schema ?? null, query], components)]);
    const owned = closed.filter((name) => uses.has(name));
    /** @type {JsonObject} */
    const file = {
      $comment: NOTICE,
      operationId,
      method,
      path,
      ...(owned.length > 0 ? { closedByClient: owned } : {}),
      bodyRequired: body?.required ?? false,
      body: body === null ? null : selfContained(body.schema, components, operationId),
      query: query === null ? null : selfContained(query, components, operationId),
    };
    const name = `${BODIES_DIR}/${operationId}.json`;
    files.set(name, await pretty(JSON.stringify(file), name));
    // Lo que se compila es el mismo esquema con las definiciones compartidas entre operaciones,
    // para que el código de cada definición se emita una sola vez.
    if (body !== null) {
      ajv.addSchema(
        /** @type {JsonObject} */ (mirror(body.schema, `${COMPONENTS_ID}#/$defs/`, operationId)),
        `body__${operationId}.json`,
      );
      exports[`body__${operationId}`] = `body__${operationId}.json`;
      bodyIds.push(operationId);
    }
    if (query !== null) {
      ajv.addSchema(
        /** @type {JsonObject} */ (mirror(query, `${COMPONENTS_ID}#/$defs/`, operationId)),
        `query__${operationId}.json`,
      );
      exports[`query__${operationId}`] = `query__${operationId}.json`;
      queryIds.push(operationId);
    }
  }
  const table = (/** @type {string} */ kind, /** @type {string[]} */ ids) =>
    `export const ${kind === 'body' ? 'bodies' : 'queries'} = Object.freeze({\n${ids.map((id) => `  ${id}: ${kind}__${id},`).join('\n')}\n});`;
  const validators = [
    '/* eslint-disable -- código autónomo de Ajv: no se edita ni se revisa a mano. */',
    '// @ts-nocheck -- código autónomo de Ajv; sus tipos están en validators.d.ts.',
    CODE_HEADER.trimEnd(),
    standalone(ajv, exports),
    table('body', bodyIds),
    table('query', queryIds),
    '',
  ].join('\n');
  files.set('src/client/validators.js', await pretty(validators, 'src/client/validators.js'));
  const union = (/** @type {string[]} */ ids) => ids.map((id) => `  | '${id}'`).join('\n');
  const declarations = `${CODE_HEADER}
/**
 * Validadores espejo compilados por Ajv 8 (el del contrato: 2020-12, estricto, sin coerción ni
 * valores por defecto) desde \`openapi/bodies/\`, en código autónomo (LC-APP-16).
 */
import type { CompiledValidator } from '@vigia/contracts';

/** Operaciones con cuerpo JSON. */
export type BodyOperationId =
${union(bodyIds)};

/** Operaciones con parámetros de consulta. */
export type QueryOperationId =
${union(queryIds)};

export declare const bodies: Readonly<Record<BodyOperationId, CompiledValidator>>;
export declare const queries: Readonly<Record<QueryOperationId, CompiledValidator>>;
`;
  files.set('src/client/validators.d.ts', await pretty(declarations, 'src/client/validators.d.ts'));
  return files;
}
