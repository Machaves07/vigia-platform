/**
 * MSW en todas las pruebas en modo error: una petición sin manejador hace fallar la prueba y
 * nunca sale a la red. En MSW 3 la opción `onUnhandledRequest` de MSW 2 se llama
 * `onUnhandledFrame` (cubre también WebSocket). Los manejadores generados desde la OpenAPI
 * llegan con VIG-286 (TASK-409); cada prueba añade los suyos con `server.use(...)`.
 */
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll } from 'vitest';

export const server = setupServer();

beforeAll(() => {
  server.listen({ onUnhandledFrame: 'error' });
});

afterEach(() => {
  server.resetHandlers();
});

afterAll(() => {
  server.close();
});
