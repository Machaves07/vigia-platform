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
 * griego, y solo letras y dígitos, lo que elimina espacios invisibles y separadores). El
 * contador «personas» con número se busca en cada nodo de texto y cada atributo por separado, y
 * después en los nodos de texto de cada elemento unidos por espacios (solo el plural seguido de
 * número), con los mismos homoglifos e invisibles plegados pero con los espacios y la puntuación
 * visibles como separadores de palabra.
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

/**
 * Longitud plegada mínima de un valor para buscarlo sin falsos positivos por azar. Por lo mismo,
 * las dos propiedades no reducen el contraejemplo (`noShrink`): la reducción lleva los valores a
 * `aaaa…` y `0000…`, que coinciden por azar con el texto unido de otros nodos y harían que el
 * mensaje nombrara un campo que no se filtró. El contraejemplo es el primero que falla, con sus
 * valores aleatorios, y se repite con su semilla.
 */
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
  readonly segments: JsonPath;
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
      segments: path,
      keys: path.filter((part): part is string => typeof part === 'string'),
      value: String(value),
    },
  ];
}

/**
 * Plegado por palabras para el contador de personas: como `fold`, pero quita solo los
 * caracteres de formato invisibles (U+200B, U+2060…) y conserva espacios y puntuación visibles
 * como separadores, para no unir palabras de nodos distintos ni de un rótulo y una fecha.
 */
function foldWords(text: string): string {
  let result = '';
  for (const char of text.normalize('NFKD').toLowerCase()) {
    if (/[\p{M}\p{Cf}]/u.test(char)) continue;
    result += /\s/u.test(char) ? ' ' : (CONFUSABLES[char] ?? char);
  }
  return result.replace(/ +/g, ' ');
}

/**
 * Contador de personas dentro de un mismo texto plegado con `foldWords`: «personas» como palabra
 * entera seguida de un número, o un número seguido de «personas» («12 personas», «(3) personas»),
 * con cualquier tramo sin letras ni dígitos entre medias («Personas (3)», «Personas — 3»,
 * «Personas: «3»»), y «1 persona». Con el número detrás se exige el plural: «Firma de la
 * persona 2026-10-24» es un rótulo y una fecha, no un contador.
 */
const PERSON_COUNTER =
  /(?<![\p{L}\p{N}])personas[^\p{L}\p{N}]*\p{N}|\p{N}[^\p{L}\p{N}]*personas(?![\p{L}\p{N}])|(?<![\p{L}\p{N}])1 persona(?![\p{L}\p{N}])/u;

/**
 * Contador partido entre los nodos de texto de un elemento, unidos por espacios («Personas
 * <span>3</span>», `<dt>Personas</dt><dd>3</dd>`): solo «personas» en plural seguida de número,
 * para no marcar un rótulo en singular junto a un número de otro nodo («Zona 3» + «Persona
 * responsable»).
 */
const SPLIT_PERSON_COUNTER = /(?<![\p{L}\p{N}])personas[^\p{L}\p{N}]*\p{N}/u;

interface CounterSource {
  readonly source: string;
  readonly text: string;
}

/**
 * Textos donde se busca el contador: cada nodo de texto, cada atributo y cada valor de
 * formulario por separado, nunca el texto concatenado de la vista.
 */
function counterSources(root: HTMLElement): CounterSource[] {
  const found: CounterSource[] = [];
  for (const node of textNodes(root)) {
    const parent = node.parentElement?.tagName.toLowerCase() ?? 'raíz';
    found.push({ source: `el texto de <${parent}>`, text: node.data });
  }
  for (const element of root.querySelectorAll('*')) {
    const tag = element.tagName.toLowerCase();
    for (const attribute of element.attributes) {
      found.push({ source: `el atributo ${attribute.name} de <${tag}>`, text: attribute.value });
    }
    if (
      element instanceof HTMLInputElement ||
      element instanceof HTMLTextAreaElement ||
      element instanceof HTMLSelectElement
    ) {
      found.push({ source: `el valor de <${tag}>`, text: element.value });
    }
  }
  return found;
}

function textNodes(root: Element): Text[] {
  const nodes: Text[] = [];
  const walker = root.ownerDocument.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node !== null; node = walker.nextNode()) {
    nodes.push(node as Text);
  }
  return nodes;
}

/**
 * Infracciones del contador en `root`: primero cada texto por separado (`PERSON_COUNTER`) y
 * después, por elemento, sus nodos de texto unidos (`SPLIT_PERSON_COUNTER`). Un elemento solo se
 * nombra si el contador no estaba ya dentro de un solo nodo o de un elemento más interno.
 */
function personCounters(root: HTMLElement): string[] {
  const found: string[] = [];
  const flagged: Node[] = [];
  for (const { source, text } of counterSources(root)) {
    if (PERSON_COUNTER.test(foldWords(text))) found.push(source);
  }
  for (const node of textNodes(root)) {
    if (PERSON_COUNTER.test(foldWords(node.data))) flagged.push(node);
  }
  // Orden inverso de documento: los descendientes antes que sus antepasados.
  for (const element of [...root.querySelectorAll('*')].reverse()) {
    if (flagged.some((node) => element.contains(node))) continue;
    const nodes = textNodes(element);
    if (nodes.length < 2) continue;
    const joined = nodes.map((node) => node.data).join(' ');
    if (SPLIT_PERSON_COUNTER.test(foldWords(joined))) {
      found.push(`el texto unido de <${element.tagName.toLowerCase()}>`);
      flagged.push(element);
    }
  }
  return found;
}

/** Nombres de campo de persona observada que ninguna vista puede declarar (BR-APP-21). */
const PERSON_FIELD_NAME =
  /person|persona|track|face|rostro|worker|trabajador|employee|empleado|document|cedula|email|correo|phone|telefono/;

/**
 * Excepción cerrada: el correo de la **cuenta de la plataforma** (no de una persona observada) es
 * campo de RV-01 (cuenta) y RV-02 (usuarios) de SCR-01 en `domain-entities.md` §3. Solo esas dos
 * vistas pueden declarar `email`, con ese nombre exacto. RV-19 añade únicamente cuatro banderas
 * de correo de la cuenta, no direcciones ni datos de persona. Cualquier otro nombre de persona
 * sigue prohibido también en ellas.
 */
const ACCOUNT_FIELDS: Readonly<Record<string, readonly string[]>> = {
  'SCR-01:RV-01': ['email'],
  'SCR-01:RV-02': ['email'],
  'SCR-15:RV-19': [
    'email_enabled_normal',
    'email_enabled_digest',
    'email_suppressed_hard_bounce',
    'email_suppressed_complaint',
  ],
};

/** Campos de `allowedFields` con nombre de persona observada. */
export function personFieldsDeclared(
  registration: Pick<ViewRegistration, 'screen' | 'view' | 'allowedFields'>,
): string[] {
  const account = ACCOUNT_FIELDS[`${registration.screen}:${registration.view}`] ?? [];
  return registration.allowedFields.filter(
    (field) => !account.includes(field) && PERSON_FIELD_NAME.test(fold(field)),
  );
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
  for (const source of personCounters(root)) {
    violations.push(`${label}: «personas» seguido de número en ${source}.`);
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

/**
 * Campos extra que se inyectan: de persona y de nombre cualquiera, nunca de `excluded`.
 *
 * Sin sesgo (`noBias`, que alcanza a todos los generadores de dentro). Con sesgo, fast-check
 * genera valores que coinciden por azar con el texto unido de otros nodos, también sesgado, y
 * eso no es una fuga:
 * - trozos como `constructor` o `__proto__` en las cadenas (semilla 1685707884: `W-constructor`
 *   frente a «…SW» + «constructor_»);
 * - enteros pegados a los límites (semilla 2142514304: `person_id` 10000001 dentro del UUID
 *   `…bfff-fff100000014` de la organización).
 */
function injectedFields(excluded: ReadonlySet<string>): fc.Arbitrary<Record<string, unknown>> {
  const person = fc.record(PERSON_VALUES, { requiredKeys: [] });
  const other = fc.dictionary(
    fc.stringMatching(/^[a-z][a-z0-9_]{2,15}$/),
    fc.oneof(token(10, 20), fc.integer({ min: 100_000_000, max: 2_147_483_647 })),
    { maxKeys: 3 },
  );
  return fc.noBias(
    fc
      .tuple(person, other)
      .map(([a, b]) =>
        Object.fromEntries(Object.entries({ ...b, ...a }).filter(([key]) => !excluded.has(key))),
      )
      .filter((fields) => Object.keys(fields).length > 0),
  );
}

export interface Session {
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
    })
    .noShrink();
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

/** Caracteres de relleno del contraste: el primero que no esté en el valor plegado. */
const FILLERS = 'qzxjkwvyfgbhmpucdlotnseriaжшщ';
const FILLER_DIGITS = '123456789';

function missingChar(chars: string, folded: string): string | undefined {
  for (const char of chars) if (!folded.includes(char)) return char;
  return undefined;
}

/**
 * `data` con el valor de `leaf` cambiado por otro del mismo tipo y la misma longitud, hecho de un
 * carácter que no está en su forma plegada: la vista recibe la misma estructura (mismos tipos y
 * mismas cadenas vacías o no), pero ninguna aparición del valor puede pasar por ese hueco.
 */
function withFiller(data: unknown, leaf: Leaf, folded: string): unknown {
  const filler = missingChar(FILLERS, folded) ?? 'ж';
  const digit = missingChar(FILLER_DIGITS, folded) ?? '1';
  const replace = (value: unknown, depth: number): unknown => {
    if (depth === leaf.segments.length) {
      if (typeof value === 'number') {
        return Number(digit.repeat(String(Math.trunc(Math.abs(value))).length));
      }
      if (typeof value === 'bigint') return BigInt(digit.repeat(String(value).length));
      return filler.repeat(leaf.value.length);
    }
    const part = leaf.segments[depth];
    if (Array.isArray(value)) {
      return value.map((item: unknown, index) =>
        index === part ? replace(item, depth + 1) : item,
      );
    }
    const record = value as Record<string, unknown>;
    const key = String(part);
    return { ...record, [key]: replace(record[key], depth + 1) };
  };
  return replace(data, 0);
}

/** Apariciones de `needle` en `text`, también solapadas. */
function occurrences(text: string, needle: string): number {
  let count = 0;
  for (let index = text.indexOf(needle); index !== -1; index = text.indexOf(needle, index + 1)) {
    count += 1;
  }
  return count;
}

/** Haystacks de `registration` pintada con `data`, ya desmontada. */
function renderedHaystacks(
  registration: ViewRegistration,
  data: Record<string, unknown>,
  context: Session,
): { readonly rendered: Haystack[]; readonly errors: ViewContractError[] } {
  const view = renderView(registration, data, context);
  try {
    return { rendered: haystacks(view.root), errors: view.errors };
  } finally {
    view.unmount();
  }
}

/**
 * Infracciones de PR-APP-10 en un ejemplo: `data` pintada en `context`, con los registros de
 * `foreignPaths` de otra organización.
 *
 * El texto de la vista se compara entero, unido entre nodos, y un valor propio seguido de un
 * rótulo fijo puede formar por azar el valor ajeno (semilla 1457963427: `reason_text`
 * «"DpropertyIsEnumerabl» + «Escalamiento del vencimiento» contiene la `description` ajena
 * «:propertyIsEnumerable»). Por eso un valor ajeno que aparece se contrasta: se vuelve a pintar
 * la vista con ese valor, y todo valor ajeno que lo contenga, cambiados por un relleno
 * (`withFiller`), y es fuga si, en alguna fuente, aparece más veces que en el contraste. Una
 * aparición por azar no pasa por los valores ajenos y sigue en el contraste; una que sale de
 * ellos desaparece con ellos. Se cambian todos a la vez porque una vista que deduplica (dos
 * registros ajenos con el mismo título pintados una vez) seguiría mostrando el otro.
 */
export function organizationLeaks(
  registration: ViewRegistration,
  data: Record<string, unknown>,
  context: Session,
  foreignPaths: readonly JsonPath[],
): string[] {
  const field = registration.organizationOf;
  if (field === undefined) {
    throw new Error(`${registration.view}: PR-APP-10 exige organizationOf.`);
  }
  const label = `${registration.view} (${registration.screen})`;
  const { rendered, errors } = renderedHaystacks(registration, data, context);
  const violations: string[] = [];
  if (foreignPaths.length > 0 && errors.length === 0) {
    violations.push(
      `${label}: ${String(foreignPaths.length)} registros de otra organización y ningún error de contrato.`,
    );
  }
  if (foreignPaths.length === 0 && errors.length > 0) {
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
  const ownValues = all.filter((leaf) => !isForeign(leaf.path)).map((leaf) => fold(leaf.value));
  const foreign = all.filter((item) => isForeign(item.path));
  for (const leaf of foreign) {
    const folded = fold(leaf.value);
    if (folded.length < MIN_FOLDED_LENGTH) continue;
    if (ownValues.some((value) => value.includes(folded))) continue;
    if (!rendered.some((haystack) => haystack.text.includes(folded))) continue;
    const sources = foreign.filter((item) => fold(item.value).includes(folded));
    const filled = sources.reduce<unknown>(
      (current, item) => withFiller(current, item, fold(item.value)),
      data,
    );
    const contrast = renderedHaystacks(
      registration,
      filled as Record<string, unknown>,
      context,
    ).rendered;
    const where = rendered.find((haystack, index) => {
      const other = contrast[index];
      const before = other?.source === haystack.source ? occurrences(other.text, folded) : 0;
      return occurrences(haystack.text, folded) > before;
    });
    if (where !== undefined) {
      violations.push(
        `${label}: «${leaf.path}» de un registro de otra organización aparece en ${where.source}.`,
      );
    }
  }
  return violations;
}

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
    })
    .noShrink();
  let exercised = 0;
  fc.assert(
    fc.property(sample, ({ data, context, foreignPaths }) => {
      if (foreignPaths.length > 0) exercised += 1;
      const violations = organizationLeaks(registration, data, context, foreignPaths);
      if (violations.length > 0) throw new Error(violations.join('\n'));
    }),
  );
  return exercised;
}
