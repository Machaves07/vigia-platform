/**
 * Lector estricto de `backend/openapi/app.yaml` (VIG-280, NFR-APP-48).
 *
 * El backend no escribe YAML arbitrario: `export_openapi.py` (`to_yaml`) emite un subconjunto
 * determinista, en bloque, con sangría de dos espacios, cada clave como cadena JSON y cada valor
 * escalar como literal JSON (`{}` y `[]` para los vacíos). Este lector acepta solo ese subconjunto
 * y falla nombrando la línea ante cualquier otra forma, así que no hace falta una dependencia de
 * YAML (presupuesto de `tech-stack-decisions.md` §9) ni puede leer en silencio algo distinto de lo
 * que el backend quiso escribir.
 */

/** @typedef {import('./types').JsonValue} JsonValue */
/** @typedef {import('./types').JsonObject} JsonObject */

/** @typedef {{ indent: number, text: string, number: number }} Line */

const KEY = /^"(?:[^"\\]|\\.)*"/;

export class OpenApiYamlError extends Error {
  /** @param {string} message */
  constructor(message) {
    super(message);
    this.name = 'OpenApiYamlError';
  }
}

/**
 * @param {Line} line
 * @param {string} message
 * @returns {never}
 */
function fail(line, message) {
  throw new OpenApiYamlError(`app.yaml, línea ${String(line.number)}: ${message}`);
}

/**
 * @param {Line} line
 * @param {string} literal
 * @returns {JsonValue}
 */
function scalar(line, literal) {
  try {
    return /** @type {JsonValue} */ (JSON.parse(literal));
  } catch {
    return fail(line, `valor que no es un literal JSON: ${literal}`);
  }
}

/**
 * Clave JSON al principio de `text` seguida de `:`; `null` si `text` no empieza por una clave.
 *
 * @param {Line} line
 * @param {string} text
 * @returns {{ key: string, rest: string } | null}
 */
function splitKey(line, text) {
  const match = KEY.exec(text);
  if (match === null || text[match[0].length] !== ':') return null;
  const key = scalar(line, match[0]);
  if (typeof key !== 'string') return fail(line, 'clave que no es una cadena');
  return { key, rest: text.slice(match[0].length + 1) };
}

class Reader {
  /** @param {Line[]} lines */
  constructor(lines) {
    /** @type {Line[]} */
    this.lines = lines;
    this.position = 0;
  }

  /** @returns {Line | undefined} */
  peek() {
    return this.lines[this.position];
  }

  /**
   * @param {number} indent
   * @returns {JsonValue}
   */
  node(indent) {
    const line = this.peek();
    if (line === undefined)
      throw new OpenApiYamlError('app.yaml: el documento termina antes de tiempo');
    if (line.indent !== indent) {
      return fail(line, `sangría ${String(line.indent)}; se esperaba ${String(indent)}`);
    }
    return line.text.startsWith('- ') ? this.list(indent) : this.mapping(indent);
  }

  /**
   * @param {number} indent
   * @returns {{ [key: string]: JsonValue }}
   */
  mapping(indent) {
    /** @type {{ [key: string]: JsonValue }} */
    const result = {};
    for (let line = this.peek(); line?.indent === indent; line = this.peek()) {
      if (line.text.startsWith('- ')) fail(line, 'elemento de lista dentro de un objeto');
      const entry = splitKey(line, line.text);
      if (entry === null) return fail(line, 'se esperaba «"clave":»');
      if (Object.hasOwn(result, entry.key)) fail(line, `clave repetida «${entry.key}»`);
      this.position += 1;
      if (entry.rest === '') {
        result[entry.key] = this.node(indent + 2);
      } else if (entry.rest.startsWith(' ')) {
        result[entry.key] = scalar(line, entry.rest.slice(1));
      } else {
        fail(line, 'falta el espacio después de «:»');
      }
    }
    return result;
  }

  /**
   * @param {number} indent
   * @returns {JsonValue[]}
   */
  list(indent) {
    /** @type {JsonValue[]} */
    const result = [];
    for (let line = this.peek(); line?.indent === indent; line = this.peek()) {
      if (!line.text.startsWith('- ')) fail(line, 'se esperaba un elemento de lista «- »');
      const rest = line.text.slice(2);
      if (rest.startsWith('- ') || splitKey(line, rest) !== null) {
        // El primer renglón de un objeto o de una lista anidados va detrás del guion: se lee
        // como si empezara dos columnas más adentro, donde siguen sus demás renglones.
        this.lines[this.position] = { indent: indent + 2, text: rest, number: line.number };
        result.push(this.node(indent + 2));
      } else {
        this.position += 1;
        result.push(scalar(line, rest));
      }
    }
    return result;
  }
}

/**
 * Lee el texto de `app.yaml` y devuelve el documento OpenAPI como valor JSON.
 *
 * @param {string} source
 * @returns {{ [key: string]: JsonValue }}
 */
export function parseOpenApiYaml(source) {
  if (source.includes('\r')) throw new OpenApiYamlError('app.yaml: fin de línea distinto de LF');
  /** @type {Line[]} */
  const lines = [];
  source.split('\n').forEach((raw, index) => {
    if (raw === '' || raw.startsWith('#')) return;
    const text = raw.trimStart();
    const indent = raw.length - text.length;
    const line = { indent, text, number: index + 1 };
    if (indent % 2 !== 0 || raw.slice(0, indent).trim() !== '') {
      fail(line, 'sangría que no es de dos espacios');
    }
    if (text !== text.trimEnd()) fail(line, 'espacio al final de la línea');
    lines.push(line);
  });
  const reader = new Reader(lines);
  const document = reader.node(0);
  const rest = reader.peek();
  if (rest !== undefined) fail(rest, 'contenido después del documento');
  if (typeof document !== 'object' || document === null || Array.isArray(document)) {
    throw new OpenApiYamlError('app.yaml: el documento no es un objeto');
  }
  return document;
}
