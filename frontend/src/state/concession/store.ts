/**
 * `ActiveConcession` (LC-APP-08, domain-entities §2.4, IF-06, BR-APP-67 a 69, PR-APP-21).
 *
 * - Solo existe para usuarios de una organización `provider`; con otra organización `activate`
 *   no hace nada y la cabecera nunca sale.
 * - `header()` es el proveedor de `X-Vigia-Concession` de `ports.base` (`transportOptions`): la
 *   concesión en **toda** petición mientras esté activa, y nunca sin ella (BR-APP-67, 69).
 * - Activar solo se puede sin otra activa (desde el panel del proveedor); cambiar de concesión
 *   solo desde el aviso permanente (`change`, UI-04), que antes vacía toda la caché de consultas
 *   (`clearAll`) y descarta los borradores (BR-APP-68).
 * - Al vencer (`expires_at` con el `Clock`) o al recibir `not_found` en una petición enviada bajo
 *   la concesión vigente, termina igual que al cambiar y avisa con `ended` para que el armazón
 *   vuelva al panel del proveedor (IF-06 paso 4).
 *
 * Vive solo en memoria de la pestaña (🧠): nada de esto se persiste.
 */
import type { Clock, TimerHandle } from '../clock';

type ScopeLevel = 'organization' | 'plant' | 'zone';

/** Concesión de `GET /provider/concessions` (`ProviderConcessionView`). */
interface ProviderConcessionLike {
  readonly concession_id: string;
  readonly client_organization_id: string;
  readonly scope_level: ScopeLevel;
  readonly scope_id: string;
  readonly expires_at: string;
  readonly status: string;
}

interface ActiveConcession {
  readonly concession_id: string;
  readonly client_organization_id: string;
  /** Nombre del cliente; llega con `GET /hierarchy` bajo la concesión (`describeClient`). */
  readonly client_organization_name: string | null;
  readonly scope_level: ScopeLevel;
  readonly scope_id: string;
  readonly scope_name: string | null;
  /** Instante ISO 8601 del vencimiento. */
  readonly expires_at: string;
  /** Motivo, cuando la vista lo trae (`ProviderConcessionView` no lo publica). */
  readonly reason: string | null;
}

/** Por qué terminó la concesión activa. */
export type ConcessionEnd = 'changed' | 'expired' | 'not_found';

export interface ConcessionSnapshot {
  readonly active: ActiveConcession | null;
  /** Último final, para el aviso del panel; `null` hasta que una concesión termine. */
  readonly ended: ConcessionEnd | null;
}

/** Lo que se usa de `ScreenQueryCache` (`state.query`). */
interface ConcessionCache {
  clearAll(): void;
}

/** Lo que se usa de `HierarchyView` (`GET /hierarchy` bajo la concesión). */
interface HierarchyLike {
  readonly organization: { readonly name: string };
  readonly plants: readonly {
    readonly plant_id: string;
    readonly name: string;
    readonly zones: readonly { readonly zone_id: string; readonly name: string }[];
  }[];
}

export interface ConcessionStore {
  readonly getSnapshot: () => ConcessionSnapshot;
  readonly subscribe: (listener: () => void) => () => void;
  /** Valor de `X-Vigia-Concession`: la concesión activa y no vencida, o `null`. */
  header(): string | null;
  /** Operar bajo una concesión activa del panel del proveedor; `false` si no procede. */
  activate(concession: ProviderConcessionLike, reason?: string | null): boolean;
  /** Nombre del cliente y del alcance, desde `GET /hierarchy` bajo la concesión. */
  describeClient(view: HierarchyLike): void;
  /** `changeConcession` de UI-04: termina la activa y vuelve al panel para elegir otra. */
  change(): void;
  /** `not_found` en una petición enviada con `concessionId` en la cabecera. */
  reportNotFound(concessionId: string): void;
  /** Un borrador se suscribe para descartarse al terminar la concesión. */
  onDiscard(discard: () => void): () => void;
  /** Cierre de sesión: termina sin aviso. */
  reset(): void;
  dispose(): void;
}

export interface ConcessionStoreOptions {
  readonly clock: Clock;
  /** `organization.kind` de `GET /me`. */
  readonly organizationKind: () => string | null;
  readonly cache: ConcessionCache;
  /** El armazón vuelve al panel del proveedor con el aviso de `reason`. */
  readonly onEnded?: (reason: ConcessionEnd) => void;
}

/** Tramo máximo del temporizador de vencimiento (1 h): por debajo del tope de `setTimeout`. */
const MAX_TIMER_MS = 3_600_000;

function expiresAtMs(concession: { readonly expires_at: string }): number {
  return Date.parse(concession.expires_at);
}

function scopeName(concession: ActiveConcession, view: HierarchyLike): string | null {
  switch (concession.scope_level) {
    case 'organization':
      return view.organization.name;
    case 'plant':
      return view.plants.find((plant) => plant.plant_id === concession.scope_id)?.name ?? null;
    case 'zone':
      for (const plant of view.plants) {
        const zone = plant.zones.find((candidate) => candidate.zone_id === concession.scope_id);
        if (zone !== undefined) return zone.name;
      }
      return null;
  }
}

export function createConcessionStore(options: ConcessionStoreOptions): ConcessionStore {
  const { clock, cache } = options;
  let snapshot: ConcessionSnapshot = Object.freeze({ active: null, ended: null });
  let timer: TimerHandle | null = null;
  const listeners = new Set<() => void>();
  const drafts = new Set<() => void>();

  const publish = (next: ConcessionSnapshot): void => {
    snapshot = Object.freeze(next);
    for (const listener of listeners) listener();
  };

  const stopTimer = (): void => {
    if (timer !== null) clock.clearTimeout(timer);
    timer = null;
  };

  /** Termina la activa: vacía la caché y los borradores, y avisa al armazón. */
  const end = (reason: ConcessionEnd): void => {
    stopTimer();
    if (snapshot.active === null) return;
    publish({ active: null, ended: reason });
    cache.clearAll();
    for (const discard of [...drafts]) discard();
    options.onEnded?.(reason);
  };

  const expired = (): boolean => {
    const active = snapshot.active;
    return active !== null && !(clock.now() < expiresAtMs(active));
  };

  /** Temporizador hasta el vencimiento, por tramos: `setTimeout` desborda pasados 24,8 días. */
  const arm = (): void => {
    const active = snapshot.active;
    if (active === null) return;
    if (expired()) {
      end('expired');
      return;
    }
    const remaining = expiresAtMs(active) - clock.now();
    timer = clock.setTimeout(
      () => {
        timer = null;
        arm();
      },
      Math.min(remaining, MAX_TIMER_MS),
    );
  };

  return {
    getSnapshot: () => snapshot,
    subscribe(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    header() {
      if (expired()) end('expired');
      return snapshot.active?.concession_id ?? null;
    },
    activate(concession, reason = null) {
      if (options.organizationKind() !== 'provider') return false;
      if (expired()) end('expired');
      if (snapshot.active !== null || concession.status !== 'active') return false;
      const deadline = expiresAtMs(concession);
      if (!Number.isFinite(deadline) || !(clock.now() < deadline)) return false;
      // Lo que se pidió sin concesión no se sirve bajo ella.
      cache.clearAll();
      publish({
        active: {
          concession_id: concession.concession_id,
          client_organization_id: concession.client_organization_id,
          client_organization_name: null,
          scope_level: concession.scope_level,
          scope_id: concession.scope_id,
          scope_name: null,
          expires_at: concession.expires_at,
          reason,
        },
        ended: null,
      });
      arm();
      return true;
    },
    describeClient(view) {
      const active = snapshot.active;
      if (active === null) return;
      publish({
        ...snapshot,
        active: {
          ...active,
          client_organization_name: view.organization.name,
          scope_name: scopeName(active, view),
        },
      });
    },
    change() {
      end('changed');
    },
    reportNotFound(concessionId) {
      // Una respuesta tardía de una concesión ya terminada no afecta a la siguiente.
      if (snapshot.active?.concession_id === concessionId) end('not_found');
    },
    onDiscard(discard) {
      drafts.add(discard);
      return () => {
        drafts.delete(discard);
      };
    },
    reset() {
      stopTimer();
      snapshot = Object.freeze({ active: null, ended: null });
      for (const listener of listeners) listener();
    },
    dispose() {
      stopTimer();
      listeners.clear();
      drafts.clear();
    },
  };
}
