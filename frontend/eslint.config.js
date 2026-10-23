/**
 * Puertas de calidad de ESLint (NFR-APP-47, BR-APP-62, NFR-APP-42, PAT-APP-RES-06).
 *
 * - `typescript-eslint` estricto con tipos, `jsx-a11y` estricto y `react-hooks`.
 * - Sin `dangerouslySetInnerHTML`, `innerHTML`/`outerHTML`/`insertAdjacentHTML`, `eval`,
 *   `new Function` ni `setTimeout`/`setInterval` con cadena (política de contenido sin
 *   `unsafe-eval` y tipos confiables, PAT-APP-SEG-01 y 04).
 * - Solo `src/ports/**` importa `src/client/**` (BR-APP-62).
 * - `no-console` como error salvo en `src/ports/base/devLog.ts` (NFR-APP-42).
 * - La hora del sistema solo se lee en `src/state/clock.ts` (PAT-APP-RES-06).
 *
 * `tests/unit/lint/rules.test.ts` comprueba que cada prohibición falla con su regla nombrada.
 */
import jsxA11y from 'eslint-plugin-jsx-a11y';
import reactHooks from 'eslint-plugin-react-hooks';
import tseslint from 'typescript-eslint';

/** Inyección de HTML (NFR-APP-47, PAT-APP-SEG-04). */
const HTML_INJECTION = [
  {
    selector: "JSXAttribute[name.name='dangerouslySetInnerHTML']",
    message: 'NFR-APP-47: prohibido dangerouslySetInnerHTML; renderiza texto con React.',
  },
  {
    selector: "Property[key.name='dangerouslySetInnerHTML']",
    message: 'NFR-APP-47: prohibido dangerouslySetInnerHTML; renderiza texto con React.',
  },
  {
    selector: 'AssignmentExpression > MemberExpression.left[property.name=/^(inner|outer)HTML$/]',
    message: 'NFR-APP-47: prohibido asignar innerHTML u outerHTML; usa textContent o React.',
  },
  {
    selector: "CallExpression[callee.property.name='insertAdjacentHTML']",
    message: 'NFR-APP-47: prohibido insertAdjacentHTML; usa textContent o React.',
  },
];

/** Hora del sistema fuera del reloj inyectable (PAT-APP-RES-06). */
const SYSTEM_TIME = [
  {
    selector: "CallExpression[callee.object.name='Date'][callee.property.name='now']",
    message: 'PAT-APP-RES-06: Date.now() solo en src/state/clock.ts; recibe un Clock.',
  },
  {
    selector: "NewExpression[callee.name='Date'][arguments.length=0]",
    message:
      'PAT-APP-RES-06: new Date() sin argumento solo en src/state/clock.ts; recibe un Clock.',
  },
  {
    selector: "CallExpression[callee.type='Identifier'][callee.name='Date']",
    message: 'PAT-APP-RES-06: Date() lee la hora del sistema; recibe un Clock.',
  },
  {
    selector: "CallExpression[callee.object.name='performance'][callee.property.name='now']",
    message: 'PAT-APP-RES-06: performance.now() solo en src/state/clock.ts; recibe un Clock.',
  },
];

const CLIENT_IMPORT = {
  patterns: [
    {
      regex: '^(@/client|(\\.{1,2}/)+(.*/)?client)(/.*)?$',
      message: 'BR-APP-62: solo src/ports/** importa src/client/**; usa el puerto de la pantalla.',
    },
  ],
};

export default tseslint.config(
  {
    ignores: ['dist/', 'coverage/', 'node_modules/', 'test-results/', 'playwright-report/'],
  },
  ...tseslint.configs.strictTypeChecked,
  ...tseslint.configs.stylisticTypeChecked,
  {
    languageOptions: {
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
    linterOptions: { reportUnusedDisableDirectives: 'error' },
  },
  {
    files: ['**/*.{ts,tsx}'],
    ...jsxA11y.flatConfigs.strict,
  },
  {
    files: ['**/*.{ts,tsx}'],
    plugins: { 'react-hooks': reactHooks },
    rules: reactHooks.configs.recommended.rules,
  },
  {
    // Globales del navegador que `no-implied-eval` vigila (`setTimeout('código')`); ESLint solo
    // los reconoce si están declarados.
    languageOptions: {
      globals: {
        window: 'readonly',
        self: 'readonly',
        globalThis: 'readonly',
        setTimeout: 'readonly',
        setInterval: 'readonly',
        execScript: 'readonly',
      },
    },
    rules: {
      'no-eval': 'error',
      'no-new-func': 'error',
      'no-implied-eval': 'error',
      '@typescript-eslint/no-implied-eval': 'error',
      'no-script-url': 'error',
      'no-console': 'error',
      'no-restricted-syntax': ['error', ...HTML_INJECTION, ...SYSTEM_TIME],
      'no-restricted-imports': ['error', CLIENT_IMPORT],
    },
  },
  {
    files: ['src/ports/**'],
    rules: { 'no-restricted-imports': 'off' },
  },
  {
    files: ['src/ports/base/devLog.ts'],
    rules: { 'no-console': 'off' },
  },
  {
    files: ['src/state/clock.ts'],
    rules: { 'no-restricted-syntax': ['error', ...HTML_INJECTION] },
  },
  {
    files: ['**/*.{js,mjs}'],
    ...tseslint.configs.disableTypeChecked,
  },
);
