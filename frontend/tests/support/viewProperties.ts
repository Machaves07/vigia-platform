/**
 * Metapropiedades de las prohibiciones de pantalla sobre una `ViewRegistration` (NFR-APP-45):
 *
 * - PR-APP-07 (BR-APP-21, 22): con campos extra inyectados (de persona y cualesquiera), el árbol
 *   renderizado no contiene el valor de ningún campo fuera de `allowedFields` ni «personas»
 *   seguido de número.
 * - PR-APP-10 (BR-APP-30): con `organization_id` mezclados, la vista no pinta nada de otra
 *   organización y registra cada caso como error de contrato.
 *
 * No se comparan subcadenas de textos fijos: se recogen los nodos de texto, los atributos y los
 * valores de formulario renderizados y se buscan en ellos los **valores** de los campos
 * prohibidos, ambos plegados (NFKD sin marcas, minúsculas, homoglifos latinos de cirílico y
 * griego, y solo letras y dígitos, lo que elimina espacios invisibles y separadores).
 */
import { render } from '@testing-library/react';
import fc from 'fast-check';

import { injectFields, objectPaths } from '../generators';
import type { ViewContractError, ViewRegistration, ViewRenderContext } from '../views/types';

type JsonPath = readonly (string | number)[];

// --- Plegado y lectura del árbol renderizado -----------------------------------------------

/** Homoglifos (ya en minúscula) que se pliegan a su letra latina. */
const CONFUSABLES: Readonly<Record<string, string>> = {
  а: 'a',
  в: 'b',
  е: 'e',
  ё: 'e',
  к: 'k',
  м: 'm',
  н: 'h',
  о: 'o',
  р: 'p',
  с: 'c',
  т: 't',
  у: 'y',
  х: 'x',
  і: 'i',
  ї: 'i',
  ј: 'j',
  ѕ: 's',
  ԁ: 'd',
  ӏ: 'l',
  ԛ: 'q',
  ԝ: 'w',
  ɡ: 'g',
  α: 'a',
  β: 'b',
  ε: 'e',
  η: 'n',
  ι: 'i',
  κ: 'k',
  ν: 'v',
  ο: 'o',
  ρ: 'p',
  τ: 't',
  υ: 'u',
  χ: 'x',
  ϲ: 'c',
  ı: 'i',
};

/** Forma plegada de un texto: la que se compara (ver cabecera). */
function fold(text: string): string {
  let result = '';
  for (const char of text.normalize('NFKD').toLowerCase()) {
    if (/\p{M}/u.test(char)) continue;
    const folded = CONFUSABLES[char] ?? char;
    if (/[\p{L}\p{N}]/u.test(folded)) result += folded;
  }
  return result;
}

/** Longitud plegada mínima de un valor para buscarlo sin falsos positivos por azar. */
const MIN_FOLDED_LENGTH = 8;

interface Haystack {
  readonly source: string;
  readonly text: string;
}

/** Texto, atributos y valores de formulario renderizados bajo `root`, plegados. */
function haystacks(root: HTMLElement): Haystack[] {
  const found: Haystack[] = [{ source: 'el texto', text: fold(root.textContent) }];
  for (const element of root.querySelectorAll('*')) {
    const tag = element.tagName.toLowerCase();
    for (const attribute of element.attributes) {
      found.push({
        source: `el atributo ${attribute.name} de <${tag}>`,
        text: fold(attribute.value),
      });
    }
    if (
      element instanceof HTMLInputElement ||
      element instanceof HTMLTextAreaElement ||
      element instanceof HTMLSelectElement
    ) {
      found.push({ source: `el valor de <${tag}>`, text: fold(element.value) });
    }
  }
  return found;
}

interface Leaf {
  readonly path: string;
  readonly keys: readonly string[];
  readonly value: string;
}

function formatPath(path: JsonPath): string {
  return path
    .map((part, index) =>
      typeof part === 'number' ? `[${String(part)}]` : index === 0 ? part : `.${part}`,
    )
    .join('');
}

/** Valores escalares de `value` con su ruta y los nombres de campo que la forman. */
function leaves(value: unknown, path: JsonPath = []): Leaf[] {
  if (Array.isArray(value)) return value.flatMap((item, index) => leaves(item, [...path, index]));
  if (typeof value === 'object' && value !== null) {
    return Object.entries(value as Record<string, unknown>).flatMap(([key, item]) =>
      leaves(item, [...path, key]),
    );
  }
  if (typeof value !== 'string' && typeof value !== 'number' && typeof value !== 'bigint') {
    return [];
  }
  return [
    {
      path: formatPath(path),
      keys: path.filter((part): part is string => typeof part === 'string'),
      value: String(value),
    },
  ];
}

/** «personas» (o «persona») junto a un número, en cualquier orden, sobre texto plegado. */
const PERSON_COUNTER = /personas?\d|\dpersonas?/;

/** Nombres de campo de persona observada que ninguna vista puede declarar (BR-APP-21). */
const PERSON_FIELD_NAME =
  /person|persona|track|face|rostro|worker|trabajador|employee|empleado|document|cedula|email|correo|phone|telefono/;

/** Campos de `allowedFields` con nombre de persona observada. */
export function personFieldsDeclared(registration: ViewRegistration): string[] {
  return registration.allowedFields.filter((field) => PERSON_FIELD_NAME.test(fold(field)));
}

/**
 * Infracciones de PR-APP-07 en el árbol `root` pintado con `data`: valores de campos fuera de
 * `allowedFields` (en cualquier tramo de su ruta) y «personas» con número. Cada mensaje nombra
 * la vista, la pantalla, el campo y dónde apareció.
 */
export function forbiddenRendered(
  registration: Pick<ViewRegistration, 'view' | 'screen' | 'allowedFields'>,
  data: unknown,
  root: HTMLElement,
): string[] {
  const label = `${registration.view} (${registration.screen})`;
  const allowed = new Set(registration.allowedFields);
  const all = leaves(data);
  const allowedValues = all
    .filter((leaf) => leaf.keys.every((key) => allowed.has(key)))
    .map((leaf) => fold(leaf.value));
  const rendered = haystacks(root);
  const violations: string[] = [];
  for (const leaf of all) {
    if (leaf.keys.every((key) => allowed.has(key))) continue;
    const folded = fold(leaf.value);
    if (folded.length < MIN_FOLDED_LENGTH) continue;
    if (allowedValues.some((value) => value.includes(folded))) continue;
    const where = rendered.find((haystack) => haystack.text.includes(folded));
    if (where !== undefined) {
      violations.push(
        `${label}: campo «${leaf.path}» fuera de allowedFields aparece en ${where.source}.`,
      );
    }
  }
  for (const haystack of rendered) {
    if (PERSON_COUNTER.test(haystack.text)) {
      violations.push(`${label}: «personas» seguido de número en ${haystack.source}.`);
    }
  }
  return violations;
}

// --- Generadores ----------------------------------------------------------------------------

const token = (min: number, max: number) =>
  fc.stringMatching(new RegExp(`^[a-z0-9]{${String(min)},${String(max)}}$`));

/** Campos de persona observada con valores plausibles y distinguibles (≥ 8 caracteres plegados). */
const PERSON_VALUES: Readonly<Record<string, fc.Arbitrary<unknown>>> = {
  person_id: fc.oneof(fc.uuid(), fc.integer({ min: 10_000_000, max: 2_147_483_647 })),
  track_id: fc.oneof(
    fc.integer({ min: 10_000_000, max: 2_147_483_647 }),
    token(8, 12).map((value) => `trk-${value}`),
  ),
  face: fc.base64String({ minLength: 16, maxLength: 64 }),
  worker_id: token(8, 12).map((value) => `W-${value}`),
  worker_name: fc.stringMatching(/^[A-Z][a-z]{4,9} [A-Z][a-z]{4,9}$/),
  document_number: fc.stringMatching(/^[0-9]{8,11}$/),
  email: fc.tuple(token(8, 12), fc.domain()).map(([user, domain]) => `${user}@${domain}`),
  phone: fc.stringMatching(/^\+57 3[0-9]{2} [0-9]{3} [0-9]{4}$/),
};

/** Campos extra que se inyectan: de persona y de nombre cualquiera, nunca de `excluded`. */
function injectedFields(excluded: ReadonlySet<string>): fc.Arbitrary<Record<string, unknown>> {
  const person = fc.record(PERSON_VALUES, { requiredKeys: [] });
  const other = fc.dictionary(
    fc.stringMatching(/^[a-z][a-z0-9_]{2,15}$/),
    fc.oneof(token(10, 20), fc.integer({ min: 100_000_000, max: 2_147_483_647 })),
    { maxKeys: 3 },
  );
  return fc
    .tuple(person, other)
    .map(([a, b]) =>
      Object.fromEntries(Object.entries({ ...b, ...a }).filter(([key]) => !excluded.has(key))),
    )
    .filter((fields) => Object.keys(fields).length > 0);
}

interface Session {
  readonly organizationId: string;
  readonly concessionOrganizationId: string | null;
}

const session: fc.Arbitrary<Session> = fc
  .record({
    organizationId: fc.uuid(),
    concessionOrganizationId: fc.option(fc.uuid(), { nil: null }),
  })
  .filter((value) => value.organizationId !== value.concessionOrganizationId);

function valueAt(value: unknown, path: JsonPath): unknown {
  let current = value;
  for (const part of path) {
    if (typeof current !== 'object' || current === null) return undefined;
    current = (current as Record<string | number, unknown>)[part];
  }
  return current;
}

/** Rutas de los objetos de `data` que tienen el campo `field`. */
function recordPaths(data: unknown, field: string): JsonPath[] {
  return objectPaths(data).filter((path) => {
    const record = valueAt(data, path);
    return typeof record === 'object' && record !== null && Object.hasOwn(record, field);
  });
}

function withOrganizations(
  data: unknown,
  field: string,
  paths: readonly JsonPath[],
  organizations: readonly string[],
): unknown {
  return paths.reduce<unknown>(
    (current, path, index) => injectFields(current, { [field]: organizations[index] }, path),
    data,
  );
}

function renderView(
  registration: ViewRegistration,
  data: Record<string, unknown>,
  context: Session,
): { readonly root: HTMLElement; readonly errors: ViewContractError[]; unmount(): void } {
  const errors: ViewContractError[] = [];
  const full: ViewRenderContext = {
    ...context,
    reportContractError: (error) => {
      errors.push(error);
    },
  };
  const { baseElement, unmount } = render(registration.render(data, full));
  return { root: baseElement, errors, unmount };
}

// --- PR-APP-07 ------------------------------------------------------------------------------

/**
 * PR-APP-07 sobre una vista: datos de la vista con campos extra inyectados en cualquier objeto
 * (si la vista tiene `organizationOf`, todos los registros son de la organización del
 * `Principal`, para que la vista los pinte). Lanza nombrando vista y campo.
 */
export function assertNoPersonData(registration: ViewRegistration): void {
  const excluded = new Set(registration.allowedFields);
  if (registration.organizationOf !== undefined) excluded.add(registration.organizationOf);
  const field = registration.organizationOf;
  const sample = fc
    .tuple(registration.data, injectedFields(excluded), fc.nat(), session)
    .map(([data, fields, pick, context]) => {
      const paths = objectPaths(data);
      let value = injectFields(data, fields, paths[pick % paths.length] ?? []);
      if (field !== undefined) {
        const records = recordPaths(value, field);
        value = withOrganizations(
          value,
          field,
          records,
          records.map(() => context.organizationId),
        );
      }
      return { data: value as Record<string, unknown>, context };
    });
  fc.assert(
    fc.property(sample, ({ data, context }) => {
      const view = renderView(registration, data, context);
      try {
        const violations = forbiddenRendered(registration, data, view.root);
        if (violations.length > 0) throw new Error(violations.join('\n'));
      } finally {
        view.unmount();
      }
    }),
  );
}

// --- PR-APP-10 ------------------------------------------------------------------------------

type Owner = 'own' | 'concession' | 'foreign_a' | 'foreign_b';

/**
 * PR-APP-10 sobre una vista con `organizationOf`: cada registro es de la organización del
 * `Principal`, de la concesión activa o de una de dos ajenas. Lanza si la vista pinta un valor
 * de un registro ajeno, si no registra ningún error de contrato habiendo registros ajenos o si
 * registra alguno sin haberlos. Devuelve cuántos ejemplos tuvieron registros ajenos.
 */
export function assertOrganizationIsolation(registration: ViewRegistration): number {
  const field = registration.organizationOf;
  if (field === undefined) {
    throw new Error(`${registration.view}: PR-APP-10 exige organizationOf.`);
  }
  const label = `${registration.view} (${registration.screen})`;
  const sample = fc
    .tuple(registration.data, session, fc.uniqueArray(fc.uuid(), { minLength: 2, maxLength: 2 }))
    .filter(
      ([, context, foreign]) =>
        !foreign.includes(context.organizationId) &&
        (context.concessionOrganizationId === null ||
          !foreign.includes(context.concessionOrganizationId)),
    )
    .chain(([data, context, foreign]) => {
      const paths = recordPaths(data, field);
      const owners: Owner[] =
        context.concessionOrganizationId === null
          ? ['own', 'foreign_a', 'foreign_b']
          : ['own', 'concession', 'foreign_a', 'foreign_b'];
      return fc
        .array(fc.constantFrom(...owners), { minLength: paths.length, maxLength: paths.length })
        .map((chosen) => {
          const organizationOf: Readonly<Record<Owner, string>> = {
            own: context.organizationId,
            concession: context.concessionOrganizationId ?? context.organizationId,
            foreign_a: foreign[0] ?? '',
            foreign_b: foreign[1] ?? '',
          };
          const organizations = chosen.map((owner) => organizationOf[owner]);
          return {
            data: withOrganizations(data, field, paths, organizations) as Record<string, unknown>,
            context,
            foreignPaths: paths.filter((_, index) => chosen[index]?.startsWith('foreign') === true),
          };
        });
    });
  let exercised = 0;
  fc.assert(
    fc.property(sample, ({ data, context, foreignPaths }) => {
      const view = renderView(registration, data, context);
      try {
        const violations: string[] = [];
        if (foreignPaths.length > 0) exercised += 1;
        if (foreignPaths.length > 0 && view.errors.length === 0) {
          violations.push(
            `${label}: ${String(foreignPaths.length)} registros de otra organización y ningún error de contrato.`,
          );
        }
        if (foreignPaths.length === 0 && view.errors.length > 0) {
          violations.push(`${label}: error de contrato sin registros de otra organización.`);
        }
        const foreignPrefixes = foreignPaths.map(formatPath);
        const isForeign = (path: string) =>
          foreignPrefixes.some(
            (prefix) =>
              path === prefix ||
              path.startsWith(`${prefix}.`) ||
              path.startsWith(`${prefix}[`) ||
              (prefix === '' && path !== ''),
          );
        const all = leaves(data);
        const ownValues = all
          .filter((leaf) => !isForeign(leaf.path))
          .map((leaf) => fold(leaf.value));
        const rendered = haystacks(view.root);
        for (const leaf of all.filter((item) => isForeign(item.path))) {
          const folded = fold(leaf.value);
          if (folded.length < MIN_FOLDED_LENGTH) continue;
          if (ownValues.some((value) => value.includes(folded))) continue;
          const where = rendered.find((haystack) => haystack.text.includes(folded));
          if (where !== undefined) {
            violations.push(
              `${label}: «${leaf.path}» de un registro de otra organización aparece en ${where.source}.`,
            );
          }
        }
        if (violations.length > 0) throw new Error(violations.join('\n'));
      } finally {
        view.unmount();
      }
    }),
  );
  return exercised;
}
