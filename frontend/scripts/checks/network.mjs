#!/usr/bin/env node
/**
 * Comprobación de `npm run check` (VIG-336; PR-APP-28 estática, NFR-APP-22, BR-APP-79): la
 * aplicación solo habla con su origen, con los orígenes prefirmados que le da el servidor y con
 * la URL local del nodo por navegación. Ningún destino de red queda escrito en el código.
 *
 * En `src/` (árbol sintáctico de TypeScript, sin comentarios):
 * - ningún texto literal contiene una URL absoluta (`https://…`, `wss://…`) ni relativa al
 *   protocolo (`//host`), salvo los identificadores inertes de `INERT` (no se piden nunca);
 * - ninguna API de red (`fetch`, `XMLHttpRequest`, `WebSocket`, `EventSource`, `sendBeacon`,
 *   trabajadores…) se nombra fuera de `src/ports/`, salvo `fetch` en la lista cerrada
 *   `OUTSIDE_PORTS`, donde cada archivo declara qué llamadas admite (ruta literal del propio
 *   origen, adaptador que reenvía sus parámetros, ninguna o solo desarrollo);
 * - fuera de `src/ports/`, ningún acceso con corchetes a un objeto global (`globalThis`, `window`,
 *   `self`, `navigator`) usa una clave calculada o el nombre de una API de red.
 *
 * En `dist/` (si existe; `npm run build` lo crea): toda URL absoluta o relativa al protocolo de
 * los archivos de texto está en `INERT` (mensajes de error de dependencias, espacios de nombres
 * XML e identificadores de esquema). Una URL nueva en la salida, sea de quien sea, falla.
 *
 * Uso: `network.mjs [carpeta src] [carpeta dist]` (las pruebas pasan otras carpetas).
 */
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { join, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

import ts from 'typescript';

const root = fileURLToPath(new URL('../..', import.meta.url));
const src = resolve(process.argv[2] ?? join(root, 'src'));
const dist = resolve(process.argv[3] ?? join(root, 'dist'));

/**
 * URL inertes: aparecen como texto pero nada las pide. Prefijos exactos, cada uno con su motivo.
 * @type {readonly { readonly prefix: string, readonly reason: string }[]}
 */
const INERT = [
  { prefix: 'https://json-schema.org/draft/2020-12/schema', reason: '$schema de los esquemas' },
  { prefix: 'https://vigia.local/schemas/', reason: '$id de los esquemas del contrato' },
  { prefix: 'http://www.w3.org/1999/xlink', reason: 'espacio de nombres XML (React)' },
  { prefix: 'http://www.w3.org/2000/svg', reason: 'espacio de nombres SVG (React, Recharts)' },
  { prefix: 'http://www.w3.org/XML/1998/namespace', reason: 'espacio de nombres XML (React)' },
  { prefix: 'http://www.w3.org/1998/Math/MathML', reason: 'espacio de nombres MathML (React)' },
  { prefix: 'http://www.w3.org/1999/xhtml', reason: 'espacio de nombres XHTML (React)' },
  { prefix: 'https://react.dev/errors/', reason: 'texto del error minificado de React' },
  { prefix: 'https://redux.js.org/Errors?code=', reason: 'texto de error de Redux (Recharts)' },
  {
    prefix: 'https://redux-toolkit.js.org/Errors?code=',
    reason: 'texto de error de Redux Toolkit',
  },
  { prefix: 'https://bit.ly/3cXEKWf', reason: 'texto del error minificado de Immer (Recharts)' },
  {
    prefix: 'https://reactrouter.com/en/main/routers/picking-a-router',
    reason: 'texto de advertencia de React Router',
  },
  {
    prefix: 'https://github.com/date-fns/date-fns/blob/master/docs/unicodeTokens.md',
    reason: 'texto de error de date-fns',
  },
  { prefix: 'http://localhost', reason: 'base de React Router para resolver rutas relativas' },
];

/** Esquemas de red: los demás (`otpauth://`, `server://` de React Router) no salen a la red. */
const NETWORK_SCHEMES = new Set(['http', 'https', 'ws', 'wss', 'ftp']);

/** URL absoluta con nombre de host (las fuentes de expresiones regulares no lo tienen). */
const ABSOLUTE = /\b([a-z][a-z0-9+.-]*):\/\/([a-z0-9-]+(?:\.[a-z0-9-]+)*)[^\s"'`<>()\\]*/gi;
/** Relativa al protocolo en atributos, CSS o textos: `"//cdn.ejemplo.org/…"`. */
const PROTOCOL_RELATIVE = /(?:^|["'`(=\s])(\/\/[a-z0-9-]+(?:\.[a-z0-9-]+)+[^\s"'`<>()\\]*)/gi;

/** APIs de red del navegador: solo `src/ports/` las nombra. */
const NETWORK_APIS = new Set([
  'fetch',
  'XMLHttpRequest',
  'WebSocket',
  'EventSource',
  'sendBeacon',
  'importScripts',
  'Worker',
  'SharedWorker',
  'serviceWorker',
  'RTCPeerConnection',
  'WebTransport',
]);

/**
 * Fuera de `src/ports/`, lista cerrada: archivo relativo a `src/` → qué llamadas a `fetch` admite y
 * por qué. `path`: el destino es `new URL('/ruta', origen)` con una ruta literal; el origen lo da
 * el armazón (el propio o el de una dependencia declarada), nunca un texto del código. `adapter`:
 * el `fetch` solo reenvía los parámetros de su función (el destino lo fija quien lo llama, con la
 * regla `path`). `none`: no se llama. `dev`: solo desarrollo, fuera de `dist/`.
 * @type {ReadonlyMap<string, { readonly calls: 'path' | 'adapter' | 'none' | 'dev', readonly reason: string }>}
 */
const OUTSIDE_PORTS = new Map([
  ['app/version.ts', { calls: 'path', reason: '`/version.json` del propio origen (LC-APP-04)' }],
  [
    'state/connection/probe.ts',
    { calls: 'path', reason: 'sonda `/health/live` del origen de cada dependencia (LC-APP-06)' },
  ],
  [
    'app/bootstrap.ts',
    { calls: 'adapter', reason: 'adaptador `fetch` de la sonda de conexión (`probe.ts`)' },
  ],
  [
    'bootstrap/check.ts',
    { calls: 'none', reason: 'detección de capacidades: comprueba que `fetch` existe' },
  ],
  [
    'mocks/browser.ts',
    { calls: 'dev', reason: 'modo simulado; fuera de `dist/` (no_msw_in_dist)' },
  ],
]);

/** ¿Los argumentos de `call` son exactamente parámetros de la función que lo envuelve? */
function forwardsParameters(/** @type {ts.CallExpression} */ call) {
  let current = call.parent;
  while (current !== undefined && !ts.isFunctionLike(current)) current = current.parent;
  if (current === undefined) return false;
  const parameters = new Set(
    current.parameters.flatMap((parameter) =>
      ts.isIdentifier(parameter.name) ? [parameter.name.text] : [],
    ),
  );
  return call.arguments.every(
    (argument) => ts.isIdentifier(argument) && parameters.has(argument.text),
  );
}

/** @param {string} url */
const inert = (url) => INERT.some(({ prefix }) => url.startsWith(prefix));

/** @param {string} directory @returns {string[]} */
function files(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) =>
    entry.isDirectory() ? files(join(directory, entry.name)) : [join(directory, entry.name)],
  );
}

/** URL de red de `value` que no son inertes. @param {string} value @returns {string[]} */
function foreignUrls(value) {
  /** @type {string[]} */
  const found = [];
  for (const match of value.matchAll(ABSOLUTE)) {
    const scheme = (match[1] ?? '').toLowerCase();
    if (NETWORK_SCHEMES.has(scheme) && !inert(match[0])) found.push(match[0]);
  }
  for (const match of value.matchAll(PROTOCOL_RELATIVE)) found.push(match[1] ?? '');
  return found;
}

/** ¿El primer argumento es `new URL('/ruta', origen)` con una ruta literal (no `//host`)? */
function sameOriginTarget(/** @type {ts.CallExpression} */ call) {
  const target = call.arguments[0];
  if (target === undefined || !ts.isNewExpression(target)) return false;
  if (!ts.isIdentifier(target.expression) || target.expression.text !== 'URL') return false;
  const path = target.arguments?.[0];
  return (
    path !== undefined &&
    ts.isStringLiteralLike(path) &&
    path.text.startsWith('/') &&
    !path.text.startsWith('//') &&
    (target.arguments?.length ?? 0) === 2
  );
}

/** Objetos globales por los que se alcanza una API de red con un acceso calculado. */
const GLOBALS = new Set(['globalThis', 'window', 'self', 'navigator']);

/**
 * Acceso con corchetes a un objeto global (`globalThis['fe' + 'tch']`, `window[nombre]`): esquiva
 * la regla de los nombres, así que fuera de `src/ports/` solo se admite con una clave literal que
 * no sea una API de red. Devuelve el problema o `null`.
 */
function computedGlobal(/** @type {ts.Node} */ node) {
  if (!ts.isElementAccessExpression(node)) return null;
  if (!ts.isIdentifier(node.expression) || !GLOBALS.has(node.expression.text)) return null;
  const key = node.argumentExpression;
  if (!ts.isStringLiteralLike(key)) return `acceso calculado a «${node.expression.text}»`;
  return NETWORK_APIS.has(key.text) ? `«${key.text}»` : null;
}

/** Nombre de la API de red que nombra `node` en posición de valor, o `null`. */
function networkName(/** @type {ts.Node} */ node) {
  if (!ts.isIdentifier(node) || !NETWORK_APIS.has(node.text)) return null;
  // Las posiciones de tipo (`typeof fetch` en un tipo, `readonly fetch: …`) no ejecutan nada.
  for (let current = node.parent; current !== undefined; current = current.parent) {
    if (ts.isTypeNode(current)) return null;
    if (ts.isStatement(current) || ts.isSourceFile(current)) break;
  }
  if (ts.isPropertySignature(node.parent) && node.parent.name === node) return null;
  return node.text;
}

/** @param {string} file @returns {string[]} */
function checkSource(file) {
  const name = relative(src, file).split(sep).join('/');
  const source = ts.createSourceFile(
    file,
    readFileSync(file, 'utf8'),
    ts.ScriptTarget.Latest,
    true,
    file.endsWith('x') ? ts.ScriptKind.TSX : undefined,
  );
  const inPorts = name.startsWith('ports/');
  const exception = OUTSIDE_PORTS.get(name);
  const allowed = exception !== undefined;
  /** @type {string[]} */
  const errors = [];
  /** @param {ts.Node} node */
  const visit = (node) => {
    if (
      ts.isStringLiteralLike(node) ||
      ts.isTemplateHead(node) ||
      ts.isTemplateMiddle(node) ||
      ts.isTemplateTail(node) ||
      ts.isJsxText(node)
    ) {
      for (const url of foreignUrls(node.text)) {
        errors.push(`src/${name}: URL absoluta «${url}» en el código.`);
      }
    }
    const api = networkName(node);
    // La lista cerrada solo admite `fetch`: cualquier otra API de red sigue fuera de `ports/`.
    if (api !== null && !inPorts && (!allowed || api !== 'fetch')) {
      errors.push(`src/${name}: «${api}» fuera de src/ports/.`);
    }
    const computed = inPorts ? null : computedGlobal(node);
    if (computed !== null) errors.push(`src/${name}: ${computed} fuera de src/ports/.`);
    if (
      exception !== undefined &&
      exception.calls !== 'dev' &&
      ts.isCallExpression(node) &&
      ((ts.isIdentifier(node.expression) && node.expression.text === 'fetch') ||
        (ts.isPropertyAccessExpression(node.expression) && node.expression.name.text === 'fetch') ||
        ts.isParenthesizedExpression(node.expression))
    ) {
      if (exception.calls === 'none') {
        errors.push(`src/${name}: llama a «fetch», y la lista cerrada solo admite nombrarlo.`);
      } else if (exception.calls === 'adapter' && !forwardsParameters(node)) {
        errors.push(`src/${name}: «fetch» que no solo reenvía los parámetros del adaptador.`);
      } else if (exception.calls === 'path' && !sameOriginTarget(node)) {
        errors.push(`src/${name}: «fetch» sin una ruta literal del propio origen.`);
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  return errors;
}

/** Archivos de texto de `dist/`; los precomprimidos derivan de ellos. */
const TEXT = /\.(?:js|mjs|cjs|css|html|json|txt|svg|webmanifest|xml|py|map)$/i;

/** @param {string} file @returns {string[]} */
function checkBuilt(file) {
  const name = relative(dist, file).split(sep).join('/');
  return [...new Set(foreignUrls(readFileSync(file, 'utf8')))].map(
    (url) => `dist/${name}: destino de red «${url}» fuera de la lista cerrada.`,
  );
}

const sources = files(src).filter((file) => /\.(?:ts|tsx|js|mjs)$/.test(file));
const errors = sources.flatMap(checkSource);
for (const name of OUTSIDE_PORTS.keys()) {
  if (!existsSync(join(src, name)) && process.argv[2] === undefined) {
    errors.push(`OUTSIDE_PORTS cita src/${name}, que no existe: quítelo de la lista.`);
  }
}
let built = 0;
if (existsSync(dist)) {
  const outputs = files(dist).filter((file) => TEXT.test(file));
  built = outputs.length;
  errors.push(...outputs.flatMap(checkBuilt));
}

if (errors.length > 0) {
  process.stderr.write(`${errors.join('\n')}\n`);
  process.exit(1);
}
process.stdout.write(
  `Destinos de red: ${String(sources.length)} fuentes de src/ y ` +
    (existsSync(dist)
      ? `${String(built)} archivos de dist/ sin destinos fuera de la lista cerrada.\n`
      : 'sin dist/ (ejecute «npm run build» para comprobar también la salida).\n'),
);
