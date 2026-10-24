/**
 * Punto de entrada: monta el enrutador (VIG-290) con el esqueleto de la aplicación mientras llega
 * la primera porción. Hasta que el arranque en tres pasos (capacidades, `GET /me`, navegación) de
 * VIG-289 (TASK-416) entregue el `Principal`, el enrutador se monta sin sesión: solo las rutas
 * públicas y `/` lleva al inicio de sesión.
 *
 * Con `VITE_API_MODE=mock` en desarrollo, antes de montar se activan los manejadores de MSW
 * generados desde `app.yaml` (`src/mocks/browser.ts`, PAT-APP-MAN-02). Al construir, Vite
 * sustituye `DEV` de `import.meta.env` por la constante `false`: la rama y su importación
 * desaparecen de `dist/` (`tests/unit/build/no_msw_in_dist.test.ts`).
 */
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import { AppRouter } from './app/router';
import './styles/base.css';
import styles from './styles/AppSkeleton.module.css';
import { text } from './texts';

function AppSkeleton() {
  return (
    <main className={styles.skeleton}>
      <h1 className={styles.title}>{text('common.app_name')}</h1>
      <p className={styles.status} role="status">
        {text('common.loading')}
      </p>
    </main>
  );
}

async function start(): Promise<void> {
  if (import.meta.env.DEV && import.meta.env.VITE_API_MODE === 'mock') {
    const { startMockApi } = await import('./mocks/browser');
    startMockApi();
  }
  const container = document.getElementById('vigia-app');
  if (container === null) throw new Error('Falta el contenedor #vigia-app en index.html.');
  createRoot(container).render(
    <StrictMode>
      <AppRouter principal={null} fallback={AppSkeleton} />
    </StrictMode>,
  );
}

void start();
