/**
 * MSW en todas las pruebas en modo error: una petición sin manejador hace fallar la prueba y
 * nunca sale a la red. En MSW 3 la opción `onUnhandledRequest` de MSW 2 se llama
 * `onUnhandledFrame` (cubre también WebSocket). El servidor trae los manejadores generados desde
 * la OpenAPI (`tests/msw/server.ts`, VIG-286); cada prueba añade los suyos con `server.use(...)`.
 */
import { afterAll, afterEach, beforeAll } from 'vitest';

import { server } from '../msw/server';

export { server };

beforeAll(() => {
  server.listen({ onUnhandledFrame: 'error' });
});

afterEach(() => {
  server.resetHandlers();
});

afterAll(() => {
  server.close();
});
