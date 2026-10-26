// @vitest-environment node
/**
 * Una sola construcción para toda organización (NFR-APP-08, PAT-APP-ESC-05): ningún archivo de
 * `src/` incrusta un UUID, un código o identificador de organización, planta o zona, ni un
 * dominio o URL fijos. Todo eso llega en ejecución desde la API (mismo origen).
 */
import { readdirSync, readFileSync } from 'node:fs';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

const ROOT = fileURLToPath(new URL('../../..', import.meta.url));
const SOURCES = join(ROOT, 'src');
const EXTENSIONS = /\.(ts|tsx|js|jsx|json|css|html|svg)$/;

interface Rule {
  readonly name: string;
  readonly pattern: RegExp;
}

const RULES: readonly Rule[] = [
  {
    name: 'UUID',
    pattern: /\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/i,
  },
  {
    // `=` es asignación en cualquier posición; `:` solo cuenta como clave de objeto (tras `{`, `,`,
    // `(` o al inicio de línea), para no confundir la rama falsa de un ternario `c ? zoneId : …`.
    name: 'identificador o código de organización, planta, zona o nodo',
    pattern:
      /\b(?:organi[sz]ation|org|tenant|customer|client|plant|planta|zone|zona|node)[_-]?(?:id|code|slug|key)\b\s*=\s*['"`][^'"`]+['"`]|(?:^|[{,(])\s*(?:organi[sz]ation|org|tenant|customer|client|plant|planta|zone|zona|node)[_-]?(?:id|code|slug|key)\b\s*:\s*['"`][^'"`]+['"`]/im,
  },
  { name: 'URL absoluta', pattern: /\b(https?|wss?):\/\/[^\s'"`)]+/i },
  {
    // Solo se eximen literales completos que sean claves del catálogo; no sus valores.
    name: 'dominio fijo',
    pattern:
      /['"`][^'"`\n]*?(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+(?:com|co|net|org|io|dev|app|cloud|es|us|aws|vigia|local|internal)(?![.\w-])/i,
  },
  { name: 'correo', pattern: /[a-z0-9._%+-]+@[a-z0-9-]+(\.[a-z0-9-]+)+/i },
];

function sourceFiles(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) return sourceFiles(path);
    return EXTENSIONS.test(entry.name) ? [path] : [];
  });
}

const TEXT_KEYS = new Set(
  sourceFiles(join(SOURCES, 'texts'))
    .filter((file) => file.endsWith('.json'))
    .flatMap((file) =>
      Object.keys(JSON.parse(readFileSync(file, 'utf8')) as Record<string, unknown>),
    ),
);

function findings(content: string, textKeys: ReadonlySet<string> = TEXT_KEYS): string[] {
  const withoutTextKeys = content.replace(
    /(['"`])([^'"`\n]*)\1/g,
    (literal: string, _quote: string, value: string) => (textKeys.has(value) ? '' : literal),
  );
  return RULES.filter((rule) =>
    rule.pattern.test(rule.name === 'dominio fijo' ? withoutTextKeys : content),
  ).map((rule) => rule.name);
}

describe('sin constantes de organización en src/', () => {
  const files = sourceFiles(SOURCES);

  it('revisa al menos los archivos del andamiaje', () => {
    const names = files.map((file) => relative(ROOT, file).replaceAll('\\', '/'));
    expect(names).toEqual(expect.arrayContaining(['src/main.tsx', 'src/texts/common.json']));
  });

  it.each(files.map((file) => [relative(ROOT, file).replaceAll('\\', '/'), file]))(
    '%s',
    (_name, file) => {
      expect(findings(readFileSync(file, 'utf8'))).toEqual([]);
    },
  );
});

describe('cada regla detecta su caso (sondas)', () => {
  it.each([
    ['UUID', "const zone = 'e3e70682-c209-4cac-a29f-6fbed82c07cd';"],
    ['identificador o código de organización, planta, zona o nodo', "const plantCode = 'PL-01';"],
    ['identificador o código de organización, planta, zona o nodo', "{ organization_id: 'acme' }"],
    ['identificador o código de organización, planta, zona o nodo', 'const org_slug = `acme`;'],
    [
      'identificador o código de organización, planta, zona o nodo',
      "config.zoneId = 'zona-norte';",
    ],
    ['identificador o código de organización, planta, zona o nodo', "{ a: 1, zone_id: 'z-1' }"],
    ['identificador o código de organización, planta, zona o nodo', "fn({ plantId: 'pl-1' })"],
    ['identificador o código de organización, planta, zona o nodo', "f(nodeId = 'nodo-1')"],
    [
      'identificador o código de organización, planta, zona o nodo',
      "const x = {\n  zoneId: 'z-1',\n};",
    ],
    ['identificador o código de organización, planta, zona o nodo', 'const zoneCode = `Z-01`;'],
    ['URL absoluta', "fetch('https://api.example.test/me');"],
    ['dominio fijo', "const host = 'app.vigia.example.com';"],
    ['dominio fijo', "const host = 'planta-norte.co';"],
    ['dominio fijo', "const message = 'Visita https://vigia.example.com para continuar';"],
    ['dominio fijo', "const key = 'unknown.key.local';"],
    ['dominio fijo', "const host = 'https://scr09.overdue.local';"],
    ['dominio fijo', '{ "scr09.overdue.local": "vigia.example.com" }'],
    ['correo', "const contact = 'soporte@cliente.co';"],
  ])('%s: %s', (rule, content) => {
    expect(findings(content)).toContain(rule);
  });

  it.each([
    "import { text } from './texts';",
    "import styles from './styles/AppSkeleton.module.css';",
    "const key = 'common.app_name';",
    "export * from './labels.es.json';",
    'const value = config.app;',
    'const zoneId = props.zoneId;',
    'return zone === undefined ? zoneId : `${zone.name} (${zone.code})`;',
    "const label = missing ? props.zoneId : 'sin nombre';",
    "const label = missing\n  ? zoneId\n  : 'sin nombre';",
    "if (zoneId === 'nuevo') return;",
    "document.getElementById('vigia-app');",
    'navigator.userAgent.includes(value)',
  ])('no confunde código normal: %s', (content) => {
    expect(findings(content)).toEqual([]);
  });
});

describe('regresión VIG-346: claves de textos, no dominios (NFR-APP-08)', () => {
  it.each(['scr01_admin.versions.app', 'scr09.overdue.local'])(
    'acepta la clave registrada %s en referencias y JSON',
    (key) => {
      expect(TEXT_KEYS.has(key)).toBe(true);
      expect(findings(`text('${key}')`)).toEqual([]);
      expect(findings(`{ key: '${key}', value: embeddedAppVersion() }`)).toEqual([]);
      expect(findings(JSON.stringify({ [key]: 'Texto traducido' }))).toEqual([]);
      expect(findings(`text(\`${key}\`)`)).toEqual([]);
    },
  );

  it.each([
    'com',
    'co',
    'net',
    'org',
    'io',
    'dev',
    'app',
    'cloud',
    'es',
    'us',
    'aws',
    'vigia',
    'local',
    'internal',
  ])('exime únicamente la clave completa registrada terminada en .%s', (tld) => {
    const key = `screen.label.${tld}`;
    const keys = new Set([key]);
    expect(findings(`text('${key}')`, keys)).toEqual([]);
    expect(findings(`const host = 'https://${key}';`, keys)).toContain('dominio fijo');
    expect(findings(`const message = 'Visita ${key}';`, keys)).toContain('dominio fijo');
    expect(findings(`const host = 'other.${tld}';`, keys)).toContain('dominio fijo');
    expect(findings(`{ "${key}": "vigia.example.com" }`, keys)).toContain('dominio fijo');
  });
});
