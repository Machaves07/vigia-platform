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
    name: 'identificador o código de organización, planta, zona o nodo',
    pattern:
      /\b(organi[sz]ation|org|tenant|customer|client|plant|planta|zone|zona|node)[_-]?(id|code|slug|key)\b\s*[:=]\s*['"`][^'"`]+['"`]/i,
  },
  { name: 'URL absoluta', pattern: /\b(https?|wss?):\/\/[^\s'"`)]+/i },
  {
    // Dentro de una cadena: `labels.es.json` o `config.app` no son dominios.
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

function findings(content: string): string[] {
  return RULES.filter((rule) => rule.pattern.test(content)).map((rule) => rule.name);
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
    ['URL absoluta', "fetch('https://api.example.test/me');"],
    ['dominio fijo', "const host = 'app.vigia.example.com';"],
    ['dominio fijo', "const host = 'planta-norte.co';"],
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
    "document.getElementById('vigia-app');",
    'navigator.userAgent.includes(value)',
  ])('no confunde código normal: %s', (content) => {
    expect(findings(content)).toEqual([]);
  });
});
