/**
 * Modo `VITE_API_MODE=mock` del servidor de desarrollo (PAT-APP-MAN-02, LC-APP-25): las
 * peticiones `fetch` a la API las responden los manejadores generados desde `app.yaml`
 * (`tests/msw/handlers.ts`) dentro de la propia página, con `getResponse` de MSW.
 *
 * Sin trabajador de servicio (PAT-APP-MAN-06): el de MSW exigiría `public/mockServiceWorker.js`,
 * que Vite copiaría a `dist/`. Lo que ningún manejador atiende sigue a la red. Solo lo carga
 * `src/main.tsx` tras comprobar `DEV` de `import.meta.env`, así que la construcción de producción no
 * incluye ni MSW ni los manejadores (`tests/unit/build/no_msw_in_dist.test.ts`).
 */
import { getResponse, isPassthroughResponse, type HttpHandler } from 'msw';

import { handlers as generated } from '../../tests/msw/handlers';

export interface MockApi {
  /** Devuelve `fetch` a la red. */
  stop(): void;
}

interface FetchTarget {
  fetch: typeof fetch;
  readonly location: { readonly href: string };
}

/** Sustituye `target.fetch` por uno que responde con `handlers` y deja pasar el resto. */
export function startMockApi(
  target: FetchTarget = globalThis,
  handlers: readonly HttpHandler[] = generated,
): MockApi {
  const network = target.fetch;
  const mocked: typeof fetch = async (input, init) => {
    const request =
      input instanceof Request && init === undefined
        ? input
        : new Request(
            input instanceof Request ? input : new URL(String(input), target.location.href),
            init,
          );
    const response = await getResponse([...handlers], request.clone());
    if (response === undefined || isPassthroughResponse(response))
      return network.call(target, request);
    return response;
  };
  target.fetch = mocked;
  return {
    stop() {
      if (target.fetch === mocked) target.fetch = network;
    },
  };
}
