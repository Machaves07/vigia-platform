/**
 * Punto de entrada (paso 2 de PAT-APP-REN-01): crea los servicios de la aplicación, instala el
 * transporte y el manejador global de fallos (UI-09) y monta el armazón (`App`, VIG-320), que
 * ejecuta el paso 3 (`GET /me` y `GET /hierarchy`) y monta el enrutador (VIG-290).
 *
 * `createRoot` recibe `rootErrorOptions`: sin ellas React escribe en la consola cada error que
 * captura un límite, también en producción (NFR-APP-18).
 *
 * Con `VITE_API_MODE=mock` en desarrollo, antes de montar se activan los manejadores de MSW
 * generados desde `app.yaml` (`src/mocks/browser.ts`, PAT-APP-MAN-02). Al construir, Vite
 * sustituye `DEV` de `import.meta.env` por la constante `false`: la rama y su importación
 * desaparecen de `dist/` (`tests/unit/build/no_msw_in_dist.test.ts`).
 */
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import { App } from './app/AppShell';
import { createAppServices } from './app/bootstrap';
import { installChunkLoadGuard } from './app/chunkLoadGuard';
import { installGlobalErrorHandler, rootErrorOptions } from './app/GlobalErrorHandler';
import { appVersion } from './app/version';
import { installTransport } from './ports/base';
import './styles/base.css';

async function start(): Promise<void> {
  if (import.meta.env.DEV && import.meta.env.VITE_API_MODE === 'mock') {
    const { startMockApi } = await import('./mocks/browser');
    startMockApi();
  }
  const container = document.getElementById('vigia-app');
  installChunkLoadGuard();
  void appVersion.start();
  if (container === null) throw new Error('Falta el contenedor #vigia-app en index.html.');
  const services = createAppServices();
  installTransport(services.transport);
  installGlobalErrorHandler(window, services.errors, services.currentRoute);
  createRoot(container, rootErrorOptions).render(
    <StrictMode>
      <App services={services} />
    </StrictMode>,
  );
}

void start();

console.log('ensayo VIG-294: el trabajo frontend debe quedar en rojo');
