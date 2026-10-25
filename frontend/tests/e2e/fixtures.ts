/* eslint-disable react-hooks/rules-of-hooks -- `use` es el relevo de fixtures de Playwright, no React. */
import { readFileSync, statSync } from 'node:fs';

import { test as base, expect, type BrowserContext, type Page } from '@playwright/test';

import { nextTotp } from './authenticator';
import { watchPolicy } from './policy';
import { readWorld, type Role, type World } from './world';

interface Account {
  readonly password: string;
  readonly totp_hex: string;
}

interface Fixtures {
  world: World;
  role: Role;
  authenticatedPage: Page;
}
interface WorkerFixtures {
  roleSession: (role: Role) => Promise<BrowserContext>;
}

export const test = base.extend<Fixtures, WorkerFixtures>({
  world: async ({}, use) => {
    await use(readWorld());
  },
  baseURL: async ({ world }, use) => {
    await use(world.base_url);
  },
  role: ['coordinator_sst', { option: true }],
  context: async ({ context }, use) => {
    const check = await watchPolicy(context);
    try {
      await use(context);
    } finally {
      check();
    }
  },
  roleSession: [
    async ({ browser }, use, workerInfo) => {
      const world = readWorld();
      const sessions = new Map<Role, BrowserContext>();
      const checks: (() => void)[] = [];
      const login = async (role: Role): Promise<BrowserContext> => {
        const reused = sessions.get(role);
        if (reused) return reused;
        expect(statSync(world.credentials_file).mode & 0o777).toBe(0o600);
        const accounts = JSON.parse(readFileSync(world.credentials_file, 'utf8')) as Record<
          Role,
          Account
        >;
        const account = accounts[role];
        // Contexto privado SIN trace, vídeo ni captura: fill() incluiría contraseña y OTP en ellos.
        const context = await browser.newContext({
          baseURL: world.base_url,
          ignoreHTTPSErrors: true,
          ...(workerInfo.project.use.viewport !== undefined
            ? { viewport: workerInfo.project.use.viewport }
            : {}),
          hasTouch: workerInfo.project.use.hasTouch ?? false,
        });
        sessions.set(role, context);
        checks.push(await watchPolicy(context));
        const page = await context.newPage();
        try {
          await page.goto('/login');
          await page.getByLabel(/correo/i).fill(world.users[role].email);
          await page.getByLabel(/contraseña/i).fill(account.password);
          await page.getByRole('button', { name: /iniciar sesión|entrar/i }).click();
          await page
            .getByLabel(/código.*(?:verificación|autenticación)|segundo factor/i)
            .fill(await nextTotp(account.totp_hex, role, world.credentials_file));
          await page.getByRole('button', { name: /verificar|confirmar/i }).click();
          await expect(page).not.toHaveURL(/\/login(?:\/|$)/);
          await page.close();
        } catch {
          await page.close();
          // No propagar errores de fill/expect que puedan adjuntar el valor secreto.
          throw new Error('No se pudo completar el acceso por interfaz con segundo factor');
        }
        return context;
      };
      try {
        await use(login);
      } finally {
        await Promise.all([...sessions.values()].map((context) => context.close()));
        for (const check of checks) check();
      }
    },
    { scope: 'worker', timeout: 180_000 },
  ],
  authenticatedPage: async ({ roleSession, role }, use) => {
    const context = await roleSession(role);
    const page = await context.newPage();
    try {
      await use(page);
    } finally {
      await page.close();
    }
  },
});

export { expect };
