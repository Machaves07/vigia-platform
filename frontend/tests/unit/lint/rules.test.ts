// @vitest-environment node
/**
 * Reglas de ESLint de la unidad (NFR-APP-47, BR-APP-62, NFR-APP-42, PAT-APP-RES-06).
 *
 * Ejecuta ESLint con `eslint.config.js` sobre fuentes en memoria (`lintText` con una ruta
 * virtual) y comprueba que cada prohibición falla con su regla nombrada, y que las excepciones
 * (`src/ports/**`, `src/ports/base/devLog.ts`, `src/state/clock.ts`) solo levantan la suya.
 */
import { fileURLToPath } from 'node:url';

import { ESLint, type Linter } from 'eslint';
import tseslint from 'typescript-eslint';
import { beforeAll, describe, expect, it } from 'vitest';

const root = fileURLToPath(new URL('../../..', import.meta.url));
let eslint: ESLint;

beforeAll(() => {
  // Las rutas virtuales no existen en disco, así que el servicio de proyectos de TypeScript no
  // las encuentra: se desactivan solo las reglas con tipos. Las que se prueban aquí son
  // sintácticas y salen tal cual de `eslint.config.js`.
  eslint = new ESLint({
    cwd: root,
    overrideConfig: [
      tseslint.configs.disableTypeChecked,
      { languageOptions: { parserOptions: { projectService: false } } },
    ],
  });
});

async function lint(filePath: string, code: string): Promise<Linter.LintMessage[]> {
  const [result] = await eslint.lintText(code, { filePath: `${root}${filePath}` });
  if (result === undefined) throw new Error('ESLint no devolvió resultado.');
  return result.messages;
}

function rules(messages: readonly Linter.LintMessage[]): string[] {
  return [...new Set(messages.map((message) => message.ruleId ?? 'fatal'))].sort();
}

const COMPONENT = 'src/components/Probe.tsx';

describe('prohibiciones con su regla nombrada', () => {
  it.each([
    [
      'un componente que importa src/client por alias',
      "import { api } from '@/client';\nexport const probe = api;\n",
      'no-restricted-imports',
      'BR-APP-62',
    ],
    [
      'un componente que importa src/client por ruta relativa',
      "import { api } from '../client/api';\nexport const probe = api;\n",
      'no-restricted-imports',
      'BR-APP-62',
    ],
    [
      'dangerouslySetInnerHTML',
      'export function Probe({ html }: { html: string }) {\n' +
        '  return <div dangerouslySetInnerHTML={{ __html: html }} />;\n}\n',
      'no-restricted-syntax',
      'dangerouslySetInnerHTML',
    ],
    [
      'innerHTML',
      'export function probe(node: HTMLElement, html: string): void {\n' +
        '  node.innerHTML = html;\n}\n',
      'no-restricted-syntax',
      'innerHTML',
    ],
    ['eval', "export const probe = (): unknown => eval('1 + 1');\n", 'no-eval', 'eval'],
    ['new Function', "export const probe = new Function('return 1');\n", 'no-new-func', 'Function'],
    [
      'setTimeout con cadena',
      "export const probe = (): void => {\n  setTimeout('probe()', 10);\n};\n",
      'no-implied-eval',
      'eval',
    ],
    [
      'console.log fuera de devLog.ts',
      "export const probe = (): void => {\n  console.log('probe');\n};\n",
      'no-console',
      'console',
    ],
    [
      'Date.now() fuera de clock.ts',
      'export const probe = (): number => Date.now();\n',
      'no-restricted-syntax',
      'Date.now()',
    ],
    [
      'new Date() sin argumento fuera de clock.ts',
      'export const probe = (): Date => new Date();\n',
      'no-restricted-syntax',
      'new Date()',
    ],
    [
      'performance.now() fuera de clock.ts',
      'export const probe = (): number => performance.now();\n',
      'no-restricted-syntax',
      'performance.now()',
    ],
  ])('falla: %s', async (_label, code, ruleId, fragment) => {
    const messages = await lint(COMPONENT, code);
    const matching = messages.filter((message) => message.ruleId === ruleId);
    expect(matching, JSON.stringify(messages, null, 2)).not.toHaveLength(0);
    expect(matching.map((message) => message.message).join('\n')).toContain(fragment);
    expect(matching.every((message) => message.severity === 2)).toBe(true);
  });

  it('una fuente limpia no tiene ningún mensaje', async () => {
    const code =
      "import { useState } from 'react';\n\n" +
      'export function Probe({ at }: { at: number }) {\n' +
      '  const [count] = useState(0);\n' +
      '  return <p>{new Date(at).toISOString()} {count}</p>;\n}\n';
    expect(await lint(COMPONENT, code)).toEqual([]);
  });

  it('react-dom/client no es src/client', async () => {
    const code =
      "import { createRoot } from 'react-dom/client';\nexport const probe = createRoot;\n";
    expect(rules(await lint('src/main.probe.tsx', code))).toEqual([]);
  });
});

describe('excepciones acotadas a su archivo', () => {
  it('src/ports/** puede importar src/client, pero no usar console', async () => {
    const code =
      "import { api } from '@/client';\nexport const probe = (): void => {\n" +
      '  console.log(api);\n};\n';
    expect(rules(await lint('src/ports/scr01/probe.ts', code))).toEqual(['no-console']);
  });

  it('src/ports/base/devLog.ts puede usar console, pero no Date.now()', async () => {
    const code = 'export const probe = (): void => {\n  console.info(Date.now());\n};\n';
    expect(rules(await lint('src/ports/base/devLog.ts', code))).toEqual(['no-restricted-syntax']);
  });

  it('src/state/clock.ts puede leer la hora, pero no inyectar HTML ni usar console', async () => {
    const time =
      'export const now = (): number => Date.now() + performance.now() + new Date().getTime();\n';
    expect(await lint('src/state/clock.ts', time)).toEqual([]);
    const html =
      'export function probe(node: HTMLElement): void {\n  node.innerHTML = String(Date.now());\n' +
      "  console.log('x');\n}\n";
    const messages = await lint('src/state/clock.ts', html);
    expect(rules(messages)).toEqual(['no-console', 'no-restricted-syntax']);
    expect(messages.find((m) => m.ruleId === 'no-restricted-syntax')?.message).toContain(
      'innerHTML',
    );
  });

  it.each(['src/components/clock.ts', 'src/state/reducers/session.ts', 'src/state/clock.test.ts'])(
    '%s no hereda la excepción de src/state/clock.ts',
    async (file) => {
      const code = 'export const now = (): number => Date.now();\n';
      expect(rules(await lint(file, code))).toEqual(['no-restricted-syntax']);
    },
  );

  it.each(['src/pages/scr01/Screen.tsx', 'src/state/connection/breaker.ts'])(
    '%s no hereda la excepción de src/ports/**',
    async (file) => {
      const code = "import { api } from '@/client';\nexport const probe = api;\n";
      expect(rules(await lint(file, code))).toEqual(['no-restricted-imports']);
    },
  );
});
