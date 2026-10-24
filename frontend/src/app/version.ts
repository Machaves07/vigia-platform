/**
 * LC-APP-05 / PAT-APP-RES-04: una discrepancia queda enclavada hasta recargar la pestaña.
 * No depende de la sesión, no navega ni toca el FormDraft. El armazón consume el snapshot;
 * su aviso visual pertenece a VIG-320. Nada se persiste en almacenamiento del navegador.
 */
import { isAccepted, isCompatible } from '@/contracts';
import { systemClock, type Clock } from '@/state/clock';
import type { FormDraft } from '@/state/reducers/formDraft';
import { text } from '@/texts';

interface ReloadContext {
  /** Ruta actual completa (pathname, búsqueda y fragmento), no la ruta de la detección. */
  readonly route: string;
  readonly draft: FormDraft | null;
}

interface VersionSnapshot {
  readonly outdated: boolean;
  readonly message: string | null;
}

interface VersionOptions {
  readonly appVersion?: string;
  readonly contractTag?: string;
  readonly clock?: Clock;
  readonly fetch?: typeof fetch;
  readonly reload?: (context: ReloadContext) => void;
}

export function createAppVersion(options: VersionOptions = {}) {
  const localVersion = options.appVersion ?? import.meta.env.VITE_APP_VERSION;
  const localContract = options.contractTag ?? import.meta.env.VITE_CONTRACT_TAG;
  const clock = options.clock ?? systemClock;
  const listeners = new Set<() => void>();
  let snapshot: VersionSnapshot = { outdated: false, message: null };
  let started = false;

  const markOutdated = (): void => {
    if (snapshot.outdated) return;
    snapshot = { outdated: true, message: text('version.outdated') };
    for (const listener of listeners) listener();
  };

  const observeApiVersion = (version: string): void => {
    if (version !== localVersion) markOutdated();
  };

  /** Lectura de metadatos públicos (no es una operación de app.yaml), acotada a 10 s. */
  const check = async (): Promise<'checked' | 'unavailable'> => {
    const controller = new AbortController();
    const isAborted = (): boolean => controller.signal.aborted;
    const timer = clock.setTimeout(() => {
      controller.abort();
    }, 10_000);
    const expired = new Promise<never>((_, reject) => {
      controller.signal.addEventListener(
        'abort',
        () => {
          reject(new Error('Se agotó la comprobación de versión.'));
        },
        { once: true },
      );
    });
    try {
      const load = async () => {
        const response = await (options.fetch ?? globalThis.fetch)(
          new URL('/version.json', globalThis.location.origin),
          { cache: 'no-store', credentials: 'same-origin', signal: controller.signal },
        );
        // Una respuesta tardía no cambia el estado tras vencer el tope.
        if (isAborted()) return;
        const apiVersion = response.headers.get('X-Vigia-Api-Version');
        if (apiVersion !== null) observeApiVersion(apiVersion);
        if (!response.ok) throw new Error('No se pudo comprobar la versión.');
        const body: unknown = await response.json();
        if (isAborted()) return;
        if (typeof body !== 'object' || body === null || Array.isArray(body)) {
          markOutdated();
          return;
        }
        const release = body as Record<string, unknown>;
        if (typeof release.app_version !== 'string' || typeof release.contract_tag !== 'string') {
          markOutdated();
          return;
        }
        observeApiVersion(release.app_version);
        try {
          const result = isCompatible(
            localContract.replace(/^v/, ''),
            release.contract_tag.replace(/^v/, ''),
            { today: new Date(clock.now()) },
          );
          if (!isAccepted(result)) markOutdated();
        } catch {
          // Una etiqueta malformada tampoco acredita compatibilidad.
          markOutdated();
        }
      };
      await Promise.race([load(), expired]);
      return 'checked';
    } catch {
      // Red caída no significa versión nueva; un bloqueo anterior nunca se levanta aquí.
      return 'unavailable';
    } finally {
      clock.clearTimeout(timer);
    }
  };

  return {
    getSnapshot: (): VersionSnapshot => snapshot,
    subscribe(listener: () => void): () => void {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    canWrite: (): boolean => !snapshot.outdated,
    observeApiVersion,
    observeMe(body: unknown): void {
      if (
        typeof body === 'object' &&
        body !== null &&
        'api_version' in body &&
        typeof body.api_version === 'string'
      )
        observeApiVersion(body.api_version);
      if (started) void check();
    },
    markOutdated,
    check,
    /** Se llama en el arranque; después, cada GET /me refresca también version.json. */
    start(): Promise<'checked' | 'unavailable'> {
      started = true;
      return check();
    },
    /**
     * Solo el manejador de la acción «recargar» llama aquí. Con dirty, el armazón confirma
     * el descarte y aplica formDraftReducer(discard) antes de solicitar de nuevo la recarga.
     * Cancelar conserva el borrador íntegro. location.reload mantiene la URL, sin redirección.
     */
    requestReload(context: ReloadContext): 'not_needed' | 'confirmation_required' | 'reloading' {
      if (!snapshot.outdated) return 'not_needed';
      if (context.draft?.dirty === true) return 'confirmation_required';
      if (options.reload === undefined) globalThis.location.reload();
      else options.reload(context);
      return 'reloading';
    },
  };
}

export type AppVersion = ReturnType<typeof createAppVersion>;
export const appVersion = createAppVersion();
