/**
 * Servidor de MSW de las pruebas (Node) con los manejadores generados de todas las operaciones de
 * `app.yaml` (LC-APP-25). `tests/setup/msw.ts` lo arranca en modo error: una petición que no es
 * de la API falla la prueba. Cada prueba añade variantes con `server.use(...)`, que
 * `resetHandlers()` quita al terminar.
 */
import { setupServer } from 'msw/node';

import { handlers } from './handlers';

export const server = setupServer(...handlers);
