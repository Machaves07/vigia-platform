/**
 * Forma de `ScreenDescriptor` (LC-APP-03, `domain-entities.md` §2.5): una constante por pantalla
 * en `src/pages/scrNN/descriptor.ts`, que `src/app/screens.ts` reúne en orden fijo.
 *
 * - `required_permissions` son claves de la matriz de U-02 ampliada por U-03 y U-04 (cualquiera de
 *   ellas basta); `scripts/checks/permissions.mjs` falla si una no está en
 *   `backend/openapi/u05-fixtures.json` (BR-APP-02).
 * - `expected_roles` y `home_for_roles` son documentales o de elección del inicio (BR-APP-04): la
 *   visibilidad nunca se calcula por rol (BR-APP-01).
 * - Cada grupo de rutas carga `src/pages/scrNN/<grupo>/index.tsx`; la tarea de la pantalla sustituye
 *   solo el relevo de su grupo. Todos los grupos de una pantalla caen en la misma porción `scrNN`
 *   (`config/build.ts`).
 */
import type { ComponentType } from 'react';

type ScreenNumber =
  | '01'
  | '02'
  | '03'
  | '04'
  | '05'
  | '06'
  | '07'
  | '08'
  | '09'
  | '10'
  | '11'
  | '12'
  | '13'
  | '14'
  | '15'
  | '16';

export type ScreenId = `SCR-${ScreenNumber}`;

/** Roles de la matriz exportada (`permissions.roles` de `u05-fixtures.json`). */
export type Role =
  | 'coordinator_sst'
  | 'administrator'
  | 'provider_installer'
  | 'plant_manager'
  | 'line_manager'
  | 'copasst'
  | 'platform_operator';

type BackendUnit = 'U-02' | 'U-03' | 'U-04' | 'U-06';

type DeviceClass = 'desktop' | 'tablet';

/** Módulo de un grupo de rutas: la página (de relevo o definitiva) como exportación por omisión. */
interface ScreenModule {
  readonly default: ComponentType;
}

/**
 * Quién alcanza un grupo de rutas:
 * - `permission`: alguna de `required_permissions` está en `effective_permissions`;
 * - `public`: sin sesión (inicio de sesión e invitación);
 * - `authenticated`: cualquier sesión, sin clave propia (la cuenta del propio usuario).
 */
type RouteAccess = 'permission' | 'public' | 'authenticated';

export interface RouteGroup {
  /** Carpeta del relevo bajo `src/pages/scrNN/`: `[a-z_]+` y, anidado, `loop/closure`. */
  readonly id: string;
  /** Rutas del grupo; la primera es su entrada. Sin parámetros si el grupo es un inicio. */
  readonly paths: readonly [string, ...string[]];
  readonly access: RouteAccess;
  /** Vacía solo si `access` no es `permission`. */
  readonly required_permissions: readonly string[];
  /** Pantallas enlazadas desde este grupo cuya porción se precarga si es el inicio. */
  readonly linked_screens?: readonly ScreenId[];
  /**
   * Solo como inicio (VIG-363, BR-APP-04): el grupo es el panel de inicio de unos roles y se abre
   * únicamente cuando es el inicio de la sesión; a cualquier otra sesión su ruta la lleva a su
   * propio inicio y el menú no lo ofrece. No es una barrera: el servidor autoriza cada lectura.
   */
  readonly home_only?: true;
  /** Entrada propia del menú (VIG-363); ver `GroupMenu`. */
  readonly menu?: GroupMenu;
  readonly load: () => Promise<ScreenModule>;
}

/**
 * Entrada propia de un grupo en el menú (VIG-363): una pantalla con grupos así aparece en el menú
 * como esas entradas, cada una con su nombre, en vez de una sola entrada con el nombre de la
 * pantalla. Si la ruta del grupo lleva zona o planta, la entrada abre un selector de las zonas o
 * plantas del alcance (`GET /hierarchy`) en lugar de una dirección.
 */
export interface GroupMenu {
  /** Clave del catálogo de textos con el nombre de la entrada (el de la página que abre). */
  readonly title_text: string;
  /** Parámetro de la ruta que elige el selector; sin él, la primera ruta sin parámetros. */
  readonly select?: 'zone_id' | 'plant_id';
}

/** Sección con permiso propio dentro de un grupo de rutas (visible si alguna clave está). */
interface ScreenSection {
  readonly id: string;
  readonly group: string;
  readonly required_permissions: readonly [string, ...string[]];
}

export interface ScreenDescriptor {
  readonly screen_id: ScreenId;
  /** Clave del catálogo de textos con el nombre de la pantalla. */
  readonly title_text: string;
  /** Ruta canónica de `frontend-components.md` §3. */
  readonly route: string;
  /** Visible en la navegación si alguna está en `effective_permissions` (BR-APP-01). */
  readonly required_permissions: readonly [string, ...string[]];
  readonly route_groups: readonly [RouteGroup, ...RouteGroup[]];
  readonly sections: readonly ScreenSection[];
  /** Documental: la visibilidad no se calcula por rol. */
  readonly expected_roles: readonly Role[];
  readonly backend_unit: BackendUnit;
  /** Roles cuyo panel de inicio es esta pantalla (BR-APP-04). */
  readonly home_for_roles: readonly Role[];
  /** Grupo de rutas que hace de inicio; `null` si y solo si `home_for_roles` está vacía. */
  readonly home_group: string | null;
  readonly device_classes: readonly [DeviceClass, ...DeviceClass[]];
  /**
   * Otras pantallas cuyos textos usa esta (VIG-364): el enrutador carga con la ruta los textos de
   * la pantalla y los de estas, antes de pintarla.
   */
  readonly texts_from?: readonly ScreenId[];
}
