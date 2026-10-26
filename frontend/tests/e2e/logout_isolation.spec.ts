import { authenticatedTest as test, expect } from './fixtures';
import { logout } from './journeys';

// Regresión de VIG-332: el logout de un recorrido no revoca el contexto del worker.
for (const role of ['coordinator_sst', 'line_manager'] as const) {
  test.describe(role, () => {
    test.setTimeout(120_000);
    test.use({ role, reuseRoleSession: false, actionTimeout: 30_000 });

    test('cerrar la sesión propia conserva la sesión compartida del rol', async ({
      authenticatedPage,
      roleSession,
    }) => {
      const shared = await roleSession(role);
      expect((await shared.request.get('/me')).status()).toBe(200);
      await authenticatedPage.goto('/home');
      await logout(authenticatedPage);
      expect((await shared.request.get('/me')).status()).toBe(200);
      expect(await roleSession(role)).toBe(shared);
    });
  });
}
