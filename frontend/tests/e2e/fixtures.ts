/* eslint-disable react-hooks/rules-of-hooks -- `use` es el relevo de fixtures de Playwright, no React. */
import { readFileSync, statSync } from 'node:fs';

import { test as base, expect, type BrowserContext, type Page } from '@playwright/test';

import { nextTotp } from './authenticator';
import { watchPolicy } from './policy';
import { assertPrivateArtifacts } from './private-artifacts';
import { readWorld, type Role, type World } from './world';

interface Account {
  readonly password: string;
  readonly totp_hex: string;
}

interface Fixtures {
  world: World;
}
interface AuthFixtures {
  role: Role;
  authenticatedPage: Page;
  _privateArtifacts: undefined;
}
interface WorkerFixtures {
  roleSession: (role: Role) => Promise<BrowserContext>;
}

const sessionPolicyChecks = new WeakMap<BrowserContext, () => void>();

export const test = base.extend<Fixtures>({
  world: async ({}, use) => {
    await use(readWorld());
  },
  baseURL: async ({ world }, use) => {
    await use(world.base_url);
  },
  context: async ({ context }, use) => {
    const check = await watchPolicy(context);
    try {
      await use(context);
    } finally {
      check();
    }
  },
});

/** Playwright instrumenta incluso browser.newContext(): el acceso requiere otra política de artefactos. */
export const authenticatedTest = test.extend<AuthFixtures, WorkerFixtures>({
  trace: 'off',
  video: 'off',
  screenshot: 'off',
  role: ['coordinator_sst', { option: true }],
  _privateArtifacts: [
    async ({ trace, video, screenshot }, use) => {
      assertPrivateArtifacts({ trace, video, screenshot });
      await use(undefined);
    },
    { auto: true },
  ],
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
        // La guarda automática exige trace/vídeo/capturas off antes de que se lea el formulario.
        const context = await browser.newContext({
          baseURL: world.base_url,
          ignoreHTTPSErrors: true,
          ...(workerInfo.project.use.viewport !== undefined
            ? { viewport: workerInfo.project.use.viewport }
            : {}),
          hasTouch: workerInfo.project.use.hasTouch ?? false,
        });
        context.setDefaultTimeout(30_000);
        sessions.set(role, context);
        const check = await watchPolicy(context, role);
        sessionPolicyChecks.set(context, check);
        checks.push(check);
        const page = await context.newPage();
        let phase = 'navegación';
        const statuses: number[] = [];
        page.on('response', (response) => {
          if (['/auth/login', '/auth/second-factor'].includes(new URL(response.url()).pathname))
            statuses.push(response.status());
        });
        try {
          await page.goto('/login');
          phase = 'formulario de acceso';
          await expect(page.getByLabel(/correo/i)).toBeVisible();
          await page.getByLabel(/correo/i).fill(world.users[role].email);
          await page.getByLabel(/contraseña/i).fill(account.password);
          await page.getByRole('button', { name: /iniciar sesión|entrar/i }).click();
          phase = 'segundo factor';
          await page
            .getByLabel(/código de 6 dígitos/i)
            .fill(await nextTotp(account.totp_hex, role, world.credentials_file));
          await page.getByRole('button', { name: /verificar|confirmar/i }).click();
          phase = 'sesión completa';
          await expect(page).not.toHaveURL(/\/login(?:\/|$)/);
          await page.close();
        } catch {
          await page.close();
          // No propagar errores de fill/expect que puedan adjuntar el valor secreto.
          throw new Error(
            `No se pudo completar el acceso por interfaz con segundo factor (${phase}; HTTP ${statuses.join(',')})`,
          );
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
      // Atribuir la violación a esta prueba, además de la guarda final del worker.
      sessionPolicyChecks.get(context)?.();
    }
  },
});

export { expect };
