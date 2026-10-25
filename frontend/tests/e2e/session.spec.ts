import { authenticatedTest as test, expect } from './fixtures';
import { roles } from './world';

// Comprueba las cuentas del mundo y el relevo de sesión, no los recorridos de negocio por rol.
for (const role of roles) {
  test.describe(role, () => {
    test.use({ role });

    test('la cuenta accede por interfaz y reutiliza su sesión con segundo factor', async ({
      authenticatedPage,
      roleSession,
      world,
    }) => {
      const context = authenticatedPage.context();
      expect(await roleSession(role)).toBe(context);
      const response = await context.request.get('/me');
      expect(response.status()).toBe(200);
      const principal: unknown = await response.json();
      expect(principal).toMatchObject({
        user: { user_id: world.users[role].user_id, email: world.users[role].email },
        assignments: [{ role }],
        second_factor_enrolled: true,
      });
    });
  });
}
