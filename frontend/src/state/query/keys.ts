/**
 * Claves de la caché (LC-APP-11): `[pantalla, operación, alcance, concesión, parámetros
 * canónicos]`. La pantalla va primero para que el límite por pantalla y la invalidación por
 * pantalla filtren por prefijo; el alcance y la concesión van en la clave para que una respuesta
 * nunca se sirva a otro alcance u otra concesión.
 */
import type { ReadOperation } from './policies';

/** Armazón (`shell`: `/me`, jerarquía, campana), panel de inicio y SCR-01 a SCR-16. */
export const SCREENS = [
  'shell',
  'home',
  'scr01',
  'scr02',
  'scr03',
  'scr04',
  'scr05',
  'scr06',
  'scr07',
  'scr08',
  'scr09',
  'scr10',
  'scr11',
  'scr12',
  'scr13',
  'scr14',
  'scr15',
  'scr16',
] as const;

export type Screen = (typeof SCREENS)[number];

/** Parámetros de una lectura: solo valores JSON, sin fechas ni funciones. */
type QueryParam =
  | string
  | number
  | boolean
  | null
  | readonly QueryParam[]
  | { readonly [name: string]: QueryParam | undefined };

export type QueryParams = Readonly<Record<string, QueryParam | undefined>>;

export type ScreenQueryKey = readonly [
  screen: Screen,
  operation: ReadOperation,
  scope: string,
  concession: string | null,
  params: QueryParams,
];

function canonical(value: QueryParam): QueryParam {
  if (Array.isArray(value)) return value.map((item: QueryParam) => canonical(item));
  if (typeof value === 'number' && !Number.isFinite(value)) {
    throw new RangeError('Parámetro de consulta no finito.');
  }
  if (value === null || typeof value !== 'object') return value;
  const entries: [string, QueryParam][] = [];
  for (const name of Object.keys(value).sort()) {
    const item = (value as Readonly<Record<string, QueryParam | undefined>>)[name];
    if (item !== undefined) entries.push([name, canonical(item)]);
  }
  return Object.fromEntries(entries);
}

/**
 * La clave de una lectura. Los parámetros se canonizan (nombres ordenados, sin `undefined`) para
 * que dos llamadas con los mismos valores compartan entrada.
 */
export function screenKey(options: {
  readonly screen: Screen;
  readonly operation: ReadOperation;
  /** Planta (`plant_id`) o `all` («todas las de mi alcance»). */
  readonly scope: string;
  /** Concesión activa (`X-Vigia-Concession`) o `null` sin concesión. */
  readonly concession: string | null;
  readonly params?: QueryParams;
}): ScreenQueryKey {
  const params = canonical(options.params ?? {}) as QueryParams;
  return [options.screen, options.operation, options.scope, options.concession, params];
}
