/**
 * `UiPreferences` (LC-APP-07, domain-entities §2.12, BR-APP-19, PR-APP-06): el único contenido
 * que la aplicación escribe en el almacenamiento del navegador, y este módulo la única escritura.
 *
 * - Una sola clave (`PREFERENCES_KEY`) con `{selected_plant_id, list_filters, columns,
 *   active_tabs}`. Antes de escribir, el valor completo se valida con el esquema cerrado
 *   (`uiPreferences.schema.json`, compilado por Ajv en `uiPreferences.validator.js`); si no
 *   cumple, no se escribe nada.
 * - De los filtros de una lista solo se recuerdan los de selección (`kind: 'select'`) cuyo valor
 *   es una de sus opciones declaradas: el texto libre y las fechas nunca salen de la memoria.
 * - Lo que se lee y no cumple el esquema se descarta y se borra, sin aviso.
 * - `clear()` (cierre de sesión) vacía el almacenamiento del origen.
 *
 * Nunca guarda identificadores de hallazgos, texto libre, borradores, tokens ni respuestas del
 * servidor. Sin almacenamiento disponible (modo privado, cuota), las preferencias viven solo en
 * memoria.
 */
import type { Screen } from '../query/keys';

import { validateUiPreferences } from './uiPreferences.validator.js';

export const PREFERENCES_KEY = 'vigia.ui_preferences';

/** Planta de la cabecera: un `plant_id` o `all` («todas las de mi alcance»). */
export type PlantChoice = 'all' | (string & {});

interface UiPreferences {
  readonly selected_plant_id?: PlantChoice;
  readonly list_filters?: Readonly<Partial<Record<Screen, Readonly<Record<string, string>>>>>;
  readonly columns?: Readonly<Partial<Record<Screen, readonly string[]>>>;
  readonly active_tabs?: Readonly<Partial<Record<Screen, string>>>;
}

/** Lo que la preferencia necesita saber de un campo de filtro (`KeysetFilterField`). */
export interface PersistableFilterField {
  readonly name: string;
  readonly kind: string;
  readonly options?: readonly { readonly value: string }[];
}

/** Lo que se usa del almacenamiento (`localStorage`). */
export type PreferencesStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem' | 'clear'>;

export interface PreferencesStore {
  read(): UiPreferences;
  selectPlant(plant: PlantChoice): void;
  /** Recuerda los filtros aplicados de una lista; solo los de selección con valor declarado. */
  rememberListFilters(
    screen: Screen,
    fields: readonly PersistableFilterField[],
    filters: Readonly<Record<string, string>>,
  ): void;
  setColumns(screen: Screen, columns: readonly string[]): void;
  setActiveTab(screen: Screen, tab: string): void;
  /** Cierre de sesión: el almacenamiento del origen queda vacío (BR-APP-19). */
  clear(): void;
}

function isUiPreferences(value: unknown): value is UiPreferences {
  return validateUiPreferences(value);
}

function parse(raw: string | null): UiPreferences | null {
  if (raw === null) return null;
  try {
    const value: unknown = JSON.parse(raw);
    return isUiPreferences(value) ? value : null;
  } catch {
    return null;
  }
}

/** `token` del esquema: un valor de lista cerrada (nunca un UUID ni texto con espacios). */
const TOKEN = /^[a-z][a-z0-9_]{0,63}$/;

/**
 * Filtros que se pueden recordar: de selección, con una de sus opciones como valor y con forma
 * de valor de lista cerrada (una opción con UUID, como una zona, no se recuerda).
 */
function persistableFilters(
  fields: readonly PersistableFilterField[],
  filters: Readonly<Record<string, string>>,
): Record<string, string> {
  const kept: Record<string, string> = {};
  for (const field of fields) {
    if (field.kind !== 'select' || !Object.hasOwn(filters, field.name)) continue;
    const value = filters[field.name];
    if (
      value !== undefined &&
      TOKEN.test(field.name) &&
      TOKEN.test(value) &&
      (field.options ?? []).some((option) => option.value === value)
    ) {
      kept[field.name] = value;
    }
  }
  return kept;
}

function withEntry<V>(
  map: Readonly<Partial<Record<Screen, V>>> | undefined,
  screen: Screen,
  value: V | null,
): Partial<Record<Screen, V>> {
  const others = Object.entries(map ?? {}).filter(([name]) => name !== screen);
  return Object.fromEntries(value === null ? others : [...others, [screen, value]]);
}

export function createPreferencesStore(storage: PreferencesStorage | null): PreferencesStore {
  const load = (): UiPreferences => {
    let raw: string | null = null;
    try {
      raw = storage?.getItem(PREFERENCES_KEY) ?? null;
    } catch {
      return {};
    }
    const value = parse(raw);
    if (value === null && raw !== null) {
      try {
        storage?.removeItem(PREFERENCES_KEY);
      } catch {
        // Sin almacenamiento utilizable: solo memoria.
      }
    }
    return value ?? {};
  };

  let current: UiPreferences = load();

  const write = (next: UiPreferences): void => {
    // Fallo cerrado: lo que no cumple el esquema no se escribe ni sustituye lo vigente.
    if (!isUiPreferences(next)) return;
    current = next;
    try {
      storage?.setItem(PREFERENCES_KEY, JSON.stringify(next));
    } catch {
      // Cuota o modo privado: la preferencia sigue en memoria.
    }
  };

  return {
    read: () => current,
    selectPlant(plant) {
      write({ ...current, selected_plant_id: plant });
    },
    rememberListFilters(screen, fields, filters) {
      const kept = persistableFilters(fields, filters);
      write({
        ...current,
        list_filters: withEntry(
          current.list_filters,
          screen,
          Object.keys(kept).length > 0 ? kept : null,
        ),
      });
    },
    setColumns(screen, columns) {
      write({ ...current, columns: withEntry(current.columns, screen, [...columns]) });
    },
    setActiveTab(screen, tab) {
      write({ ...current, active_tabs: withEntry(current.active_tabs, screen, tab) });
    },
    clear() {
      current = {};
      try {
        storage?.clear();
      } catch {
        // Nada que borrar.
      }
    },
  };
}
