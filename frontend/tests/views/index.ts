/**
 * Vistas de lectura de ejemplo (RV-01 a 20) por descubrimiento: cada tarea añade su
 * `tests/views/<vista>.ts` con un `export default { nombre: vista }` y no edita este archivo.
 */
import { aggregateModules } from '../support/aggregate';

export const views = aggregateModules<unknown>(
  'tests/views',
  import.meta.glob<unknown>(['./*.ts', '!./index.ts'], { eager: true, import: 'default' }),
);
