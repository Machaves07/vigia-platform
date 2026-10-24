/**
 * Vistas de lectura de ejemplo (RV-01 a 20) por descubrimiento: cada tarea añade su
 * `tests/views/<vista>.ts` con un `export default { nombre: registro }` (`ViewRegistration` de
 * `./types`) y no edita este archivo. `types.ts` y los archivos `_*.ts` (la vista de autoprueba
 * de PR-APP-07 y 10) no son vistas y quedan fuera.
 */
import { aggregateModules } from '../support/aggregate';
import type { ViewRegistration } from './types';

export const views = aggregateModules<ViewRegistration>(
  'tests/views',
  import.meta.glob<unknown>(['./*.ts', '!./index.ts', '!./types.ts', '!./_*.ts'], {
    eager: true,
    import: 'default',
  }),
);
