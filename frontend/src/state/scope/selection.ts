/**
 * `ScopeSelection` (LC-APP-07, domain-entities §2.3, BR-APP-05 a 07, NFR-APP-10).
 *
 * - `plantId`: la planta de la cabecera o `all` («todas las de mi alcance», por omisión). Se
 *   restaura de `UiPreferences` y persiste entre pantallas y sesiones (BR-APP-05).
 * - `availablePlants`: las plantas de `GET /hierarchy`, ya acotadas por el servidor al alcance
 *   de la sesión (o de la concesión). Hasta que llegan, la selección vale `all`; si la planta
 *   guardada no está entre ellas, la selección vuelve a `all` sin aviso ni error (BR-APP-06).
 * - `zones`: filtro de zona **por pantalla**, solo en memoria; nunca global ni persistido. Al
 *   cambiar de planta se vacía.
 * - `scopeParams(screen)`: lo que toda lista envía como filtro explícito, `plant_id` y `zone_id`;
 *   nunca la organización, que el servidor fija por sesión (BR-APP-07). Con «todas», no envía
 *   planta: el servidor pagina sobre todo el alcance y la interfaz no concatena listas de varias
 *   plantas (NFR-APP-10).
 */
import type { Screen } from '../query/keys';

import type { PlantChoice, PreferencesStore } from './preferences';

/** Planta del alcance (domain-entities §2.3). */
export interface AvailablePlant {
  readonly plant_id: string;
  readonly code: string;
  readonly name: string;
  readonly timezone: string;
  readonly country: string;
}

export interface ScopeSnapshot {
  readonly plantId: PlantChoice;
  /** `null` mientras `GET /hierarchy` no haya respondido. */
  readonly availablePlants: readonly AvailablePlant[] | null;
  readonly zones: Readonly<Partial<Record<Screen, string>>>;
}

/** Filtro explícito de alcance de una lista (BR-APP-07). */
interface ScopeParams {
  readonly plant_id?: string;
  readonly zone_id?: string;
}

export interface ScopeStore {
  readonly getSnapshot: () => ScopeSnapshot;
  readonly subscribe: (listener: () => void) => () => void;
  /** Plantas de `GET /hierarchy` (`plantsFromHierarchy`). */
  setAvailablePlants(plants: readonly AvailablePlant[]): void;
  /** `plantChanged` de UI-03: una planta del alcance o `all`; otra cosa se ignora. */
  selectPlant(plant: PlantChoice): void;
  /** Zona de una pantalla, o `null` para quitarla. */
  setZone(screen: Screen, zoneId: string | null): void;
  scopeParams(screen: Screen): ScopeParams;
  /** Cierre de sesión o cambio de concesión: vuelve a `all` y olvida las plantas y zonas. */
  reset(): void;
}

/** Lo que se usa de `HierarchyView` (`GET /hierarchy`). */
interface HierarchyLike {
  readonly plants: readonly AvailablePlant[];
}

/** `available_plants` de una respuesta de `GET /hierarchy`, sin los campos que no se usan. */
export function plantsFromHierarchy(view: HierarchyLike): AvailablePlant[] {
  return view.plants.map(({ plant_id, code, name, timezone, country }) => ({
    plant_id,
    code,
    name,
    timezone,
    country,
  }));
}

const ALL: PlantChoice = 'all';
const EMPTY: ScopeSnapshot = Object.freeze({ plantId: ALL, availablePlants: null, zones: {} });

export function createScopeStore(preferences: PreferencesStore): ScopeStore {
  let snapshot: ScopeSnapshot = EMPTY;
  const listeners = new Set<() => void>();

  const publish = (next: ScopeSnapshot): void => {
    snapshot = Object.freeze(next);
    for (const listener of listeners) listener();
  };

  const inScope = (plant: PlantChoice, plants: readonly AvailablePlant[]): boolean =>
    plant === 'all' || plants.some((candidate) => candidate.plant_id === plant);

  return {
    getSnapshot: () => snapshot,
    subscribe(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    setAvailablePlants(plants) {
      const saved = preferences.read().selected_plant_id ?? 'all';
      const plantId = inScope(saved, plants) ? saved : 'all';
      // BR-APP-06: la planta guardada fuera del alcance se olvida en silencio.
      if (plantId !== saved) preferences.selectPlant('all');
      publish({
        plantId,
        availablePlants: Object.freeze([...plants]),
        zones: plantId === snapshot.plantId ? snapshot.zones : {},
      });
    },
    selectPlant(plant) {
      const plants = snapshot.availablePlants;
      if (plants === null || !inScope(plant, plants)) return;
      if (plant === snapshot.plantId) return;
      preferences.selectPlant(plant);
      publish({ ...snapshot, plantId: plant, zones: {} });
    },
    setZone(screen, zoneId) {
      const others = Object.entries(snapshot.zones).filter(([name]) => name !== screen);
      const zones = Object.fromEntries(
        zoneId === null || zoneId === '' ? others : [...others, [screen, zoneId]],
      ) as Partial<Record<Screen, string>>;
      publish({ ...snapshot, zones });
    },
    scopeParams(screen) {
      const zone = snapshot.zones[screen];
      return {
        ...(snapshot.plantId === 'all' ? {} : { plant_id: snapshot.plantId }),
        ...(zone === undefined ? {} : { zone_id: zone }),
      };
    },
    reset() {
      publish(EMPTY);
    },
  };
}
