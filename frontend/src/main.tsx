/**
 * Punto de entrada: monta el esqueleto de la aplicación. El arranque en tres pasos (capacidades,
 * `GET /me`, navegación) llega con VIG-289 (TASK-416) y el armazón con VIG-290.
 */
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

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

const container = document.getElementById('vigia-app');
if (container === null) throw new Error('Falta el contenedor #vigia-app en index.html.');

createRoot(container).render(
  <StrictMode>
    <AppSkeleton />
  </StrictMode>,
);
