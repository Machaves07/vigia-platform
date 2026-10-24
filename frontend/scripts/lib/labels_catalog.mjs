/** Cobertura independiente de las etiquetas: OpenAPI y tipos generados del contrato fijado. */
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import ts from 'typescript';
import { labelsEs } from '@vigia/contracts';

import { parseOpenApiYaml } from './openapi_yaml.mjs';

/** @param {string} value */
const snake = (value) => value.replace(/([a-z0-9])([A-Z])/g, '$1_$2').toLowerCase();

/** @typedef {{ name: string, values: string[], origin: string }} Enumeration */
/** @typedef {Record<string, Record<string, string>>} Labels */

/** @param {string} path @returns {unknown} */
const json = (path) => JSON.parse(readFileSync(path, 'utf8'));

/** Combina por valor: U-01 y U-02 comparten nombres con conjuntos distintos.
 * @param {Labels[]} sources @returns {Labels}
 */
export function mergeLabels(sources) {
  /** @type {Labels} */
  const result = Object.create(null);
  for (const source of sources) {
    for (const [name, values] of Object.entries(source)) {
      result[name] = { ...result[name], ...values };
    }
  }
  return result;
}

/** @param {string} root @param {string} resources @returns {Labels} */
export function loadLabels(root, resources) {
  /** @type {Labels} */
  const app = Object.create(null);
  for (const [key, caption] of Object.entries(
    /** @type {Record<string, string>} */ (json(join(root, 'src/texts/app_labels.json'))),
  )) {
    const [prefix, name, value, extra] = key.split('.');
    if (prefix !== 'labels' || !name || !value || extra !== undefined) {
      throw new Error(`Clave de etiqueta inválida: ${key}`);
    }
    app[name] ??= {};
    app[name][value] = caption;
  }
  return mergeLabels([
    labelsEs,
    /** @type {Labels} */ (json(join(resources, 'labels.platform.es.json'))),
    /** @type {Labels} */ (json(join(resources, 'labels.loop.es.json'))),
    app,
  ]);
}

/** Descubre también enums inline; no usa las etiquetas para decidir qué comprobar.
 * @param {unknown} document @returns {Enumeration[]}
 */
export function openApiEnums(document) {
  /** @type {Enumeration[]} */
  const found = [];
  /** @param {unknown} node @param {string[]} path */
  function walk(node, path) {
    if (typeof node !== 'object' || node === null) return;
    if ('enum' in node && Array.isArray(node.enum)) {
      const origin = path.join('.');
      const schema = path.indexOf('schemas');
      const parts = schema >= 0 ? path.slice(schema + 1) : path;
      const name = parts
        .filter((part) => part !== 'properties')
        .map(snake)
        .join('_');
      if (!node.enum.every((value) => typeof value === 'string')) {
        throw new Error(`Enumeración no textual sin política de pantalla: ${origin}`);
      }
      found.push({ name, values: node.enum, origin });
    }
    for (const [key, value] of Object.entries(node)) walk(value, [...path, key]);
  }
  walk(document, []);
  return found;
}

/** El paquete no exporta los esquemas fuente: sus uniones literales generadas son la frontera
 * tipada que consume U-05. Se usa el AST, no regex sobre TypeScript ni los valores de labelsEs.
 * @param {string} source @returns {Enumeration[]}
 */
export function contractEnums(source) {
  const file = ts.createSourceFile('contracts.d.ts', source, ts.ScriptTarget.Latest, true);
  /** @type {Enumeration[]} */
  const found = [];
  /** @param {import('typescript').Node} node @param {string[]} path */
  function walk(node, path) {
    const named =
      ts.isTypeAliasDeclaration(node) ||
      ts.isInterfaceDeclaration(node) ||
      ts.isPropertySignature(node);
    const next = named && node.name ? [...path, node.name.getText(file).replaceAll('"', '')] : path;
    if (
      ts.isUnionTypeNode(node) &&
      node.types.every((item) => ts.isLiteralTypeNode(item) && ts.isStringLiteral(item.literal))
    ) {
      found.push({
        name: next.map(snake).join('_'),
        values: node.types.map(
          (item) =>
            /** @type {import('typescript').StringLiteral} */ (
              /** @type {import('typescript').LiteralTypeNode} */ (item).literal
            ).text,
        ),
        origin: `contract.${next.join('.')}`,
      });
    }
    ts.forEachChild(node, (child) => walk(child, next));
  }
  walk(file, []);
  if (found.length === 0)
    throw new Error('El contrato no contiene enumeraciones; revisar el lector.');
  return found;
}

// A-42: exclusivamente MIME de transporte; no se muestran como etiquetas. No se excluyen
// estados de autenticación ni enums desconocidos. Una lista nueva siempre exige etiqueta.
const TECHNICAL = new Set([
  'content_type',
  'document_content_type',
  'clip_reference_content_type',
  'required_headers_content-type',
  'clip_upload_request_content_type',
  'video_clip_content_type',
]);
const ALIASES = new Map([['grant_body_scope_level', 'scope_level']]);

/** @param {Enumeration[]} enumerations @param {Labels} labels @returns {string[]} */
export function missingLabels(enumerations, labels) {
  const errors = [];
  for (const entry of enumerations) {
    if (TECHNICAL.has(entry.name)) continue;
    const name = ALIASES.get(entry.name) ?? entry.name;
    const values = Object.hasOwn(labels, name) ? labels[name] : undefined;
    const covered = new Set(
      Object.entries(values ?? {})
        .filter(([, caption]) => typeof caption === 'string' && caption.trim() !== '')
        .map(([value]) => value),
    );
    for (const value of new Set(entry.values)) {
      if (!covered.has(value)) errors.push(`${entry.origin}: ${name}.${value} sin etiqueta`);
    }
  }
  return errors;
}

/** @param {string} root @returns {Enumeration[]} */
export function loadEnumerations(root) {
  const require = createRequire(join(root, 'package.json'));
  const contract = dirname(require.resolve('@vigia/contracts/package.json'));
  const app = parseOpenApiYaml(readFileSync(join(root, '../backend/openapi/app.yaml'), 'utf8'));
  return [
    ...openApiEnums(app),
    ...contractEnums(readFileSync(join(contract, 'dist/types/contracts.d.ts'), 'utf8')),
  ];
}
