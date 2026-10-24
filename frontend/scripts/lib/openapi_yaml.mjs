/**
 * Lector estricto del YAML que escribe `export_openapi.to_yaml` del backend
 * (`backend/openapi/app.yaml`), sin dependencias (VIG-286; presupuesto de `tech-stack-decisions.md`
 * §9: `yaml` y `js-yaml` solo llegan como dependencias transitivas).
 *
 * El dialecto es un subconjunto de YAML en bloque: sangría de dos espacios, claves entre comillas
 * y todo escalar escrito como JSON (`"texto"` con escapes JSON, números, `true`, `false`, `null`,
 * `{}` y `[]` para los vacíos). Cualquier otra forma detiene la lectura con la línea: si el
 * exportador cambia de dialecto, este lector falla en lugar de leer otra cosa.
 */

/**
 * Un valor JSON; `object` cubre listas y mapas (JSDoc no admite el alias recursivo).
 * @typedef {null | boolean | number | string | object} JsonValue
 * @typedef {{ indent: number, text: string, number: number }} Line
 */

/** Error de lectura con el número de línea de `app.yaml`. */
export class OpenApiYamlError extends Error {
  /**
   * @param {string} message
   * @param {number} line
   */
  constructor(message, line) {
    super(`app.yaml, línea ${String(line)}: ${message}`);
    this.name = 'OpenApiYamlError';
  }
}

/**
 * Lee el documento completo.
 *
 * @param {string} text
 * @returns {JsonValue}
 */
export function parseOpenApiYaml(text) {
  /** @type {Line[]} */
  const lines = [];
  text.split('\n').forEach((raw, index) => {
    const number = index + 1;
    if (raw === '' || raw.startsWith('#')) return;
    if (raw.includes('\t') || raw.endsWith(' ') || raw.endsWith('\r')) {
      throw new OpenApiYamlError('tabulador, espacio final o retorno de carro', number);
    }
    const indent = raw.length - raw.trimStart().length;
    if (indent % 2 !== 0) throw new OpenApiYamlError('sangría impar', number);
    lines.push({ indent, text: raw.slice(indent), number });
  });
  if (lines.length === 0) throw new OpenApiYamlError('documento vacío', 1);
  const state = { lines, position: 0 };
  const first = lines[0];
  if (first === undefined || first.indent !== 0) {
    throw new OpenApiYamlError('el documento empieza sangrado', first?.number ?? 1);
  }
  const value = parseBlock(state, 0);
  const rest = lines[state.position];
  if (rest !== undefined) throw new OpenApiYamlError('contenido fuera del documento', rest.number);
  return value;
}

/**
 * @param {{ lines: Line[], position: number }} state
 * @param {number} indent
 * @returns {JsonValue}
 */
function parseBlock(state, indent) {
  const line = state.lines[state.position];
  if (line === undefined) throw new OpenApiYamlError('falta un bloque', lastNumber(state));
  if (line.indent !== indent) throw new OpenApiYamlError('sangría inesperada', line.number);
  return line.text.startsWith('-') ? parseSequence(state, indent) : parseMapping(state, indent);
}

/**
 * @param {{ lines: Line[], position: number }} state
 * @param {number} indent
 * @returns {JsonValue[]}
 */
function parseSequence(state, indent) {
  /** @type {JsonValue[]} */
  const items = [];
  for (;;) {
    const line = state.lines[state.position];
    if (line === undefined || line.indent < indent) return items;
    if (line.indent > indent) throw new OpenApiYamlError('sangría inesperada', line.number);
    if (!line.text.startsWith('- ')) {
      throw new OpenApiYamlError('se esperaba un elemento «- » de la lista', line.number);
    }
    const content = line.text.slice(2);
    const key = readKey(content, line.number);
    if (key === null && !content.startsWith('- ')) {
      items.push(parseScalar(content, line.number));
      state.position += 1;
      continue;
    }
    // Un mapa o una lista dentro del elemento: su primera línea sigue al guion, dos columnas más
    // adentro; se reescribe como una línea sangrada y se lee como bloque.
    state.lines[state.position] = { indent: indent + 2, text: content, number: line.number };
    items.push(parseBlock(state, indent + 2));
  }
}

/**
 * @param {{ lines: Line[], position: number }} state
 * @param {number} indent
 * @returns {{ [key: string]: JsonValue }}
 */
function parseMapping(state, indent) {
  /** @type {{ [key: string]: JsonValue }} */
  const mapping = {};
  for (;;) {
    const line = state.lines[state.position];
    if (line === undefined || line.indent < indent) return mapping;
    if (line.indent > indent) throw new OpenApiYamlError('sangría inesperada', line.number);
    const key = readKey(line.text, line.number);
    if (key === null) throw new OpenApiYamlError('se esperaba una clave «"…":»', line.number);
    if (Object.hasOwn(mapping, key.name)) {
      throw new OpenApiYamlError(`clave repetida «${key.name}»`, line.number);
    }
    state.position += 1;
    if (key.rest === '') {
      const next = state.lines[state.position];
      if (next === undefined || next.indent <= indent) {
        throw new OpenApiYamlError(`«${key.name}» sin valor`, line.number);
      }
      Object.defineProperty(mapping, key.name, {
        value: parseBlock(state, indent + 2),
        enumerable: true,
        writable: true,
        configurable: true,
      });
    } else {
      if (!key.rest.startsWith(' ')) {
        throw new OpenApiYamlError('falta el espacio tras «:»', line.number);
      }
      Object.defineProperty(mapping, key.name, {
        value: parseScalar(key.rest.slice(1), line.number),
        enumerable: true,
        writable: true,
        configurable: true,
      });
    }
  }
}

/**
 * Clave JSON seguida de «:» al principio de `text`, o `null` si `text` no empieza por una clave.
 *
 * @param {string} text
 * @param {number} number
 * @returns {{ name: string, rest: string } | null}
 */
function readKey(text, number) {
  if (!text.startsWith('"')) return null;
  const end = stringEnd(text, number);
  if (text[end] !== ':') return null;
  return {
    name: /** @type {string} */ (parseScalar(text.slice(0, end), number)),
    rest: text.slice(end + 1),
  };
}

/**
 * Posición siguiente a la comilla que cierra la cadena JSON que empieza en `text[0]`.
 *
 * @param {string} text
 * @param {number} number
 * @returns {number}
 */
function stringEnd(text, number) {
  for (let index = 1; index < text.length; index += 1) {
    const char = text[index];
    if (char === '\\') index += 1;
    else if (char === '"') return index + 1;
  }
  throw new OpenApiYamlError('cadena sin cerrar', number);
}

/**
 * @param {string} text
 * @param {number} number
 * @returns {JsonValue}
 */
function parseScalar(text, number) {
  if (text === '{}') return {};
  if (text === '[]') return [];
  if (text.startsWith('"') && stringEnd(text, number) !== text.length) {
    throw new OpenApiYamlError('texto tras la cadena', number);
  }
  if (!/^(?:".*"|-?\d+(?:\.\d+)?(?:e[+-]?\d+)?|true|false|null)$/s.test(text)) {
    throw new OpenApiYamlError(`escalar fuera del dialecto: ${text.slice(0, 40)}`, number);
  }
  try {
    return /** @type {JsonValue} */ (JSON.parse(text));
  } catch {
    throw new OpenApiYamlError(`escalar JSON no válido: ${text.slice(0, 40)}`, number);
  }
}

/**
 * @param {{ lines: Line[] }} state
 * @returns {number}
 */
function lastNumber(state) {
  return state.lines.at(-1)?.number ?? 1;
}
