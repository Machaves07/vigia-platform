import { readFileSync } from 'node:fs';

import { test, expect } from './fixtures';
import { roles } from './world';

test('la construcción real carga con la política de producción activa', async ({
  page,
  request,
  world,
}) => {
  expect(Object.keys(world.users).sort()).toEqual([...roles].sort());
  const index = await request.get('/', { headers: { Accept: 'text/html' } });
  expect(index.status()).toBe(200);
  expect(index.headers()['cache-control']).toBe('no-store');
  expect(await index.text()).toContain('vigia-app');
  const expectedVersion: unknown = JSON.parse(
    readFileSync(new URL('../../dist/version.json', import.meta.url), 'utf8'),
  );
  const version = await request.get('/version.json');
  expect(version.status()).toBe(200);
  expect(await version.json()).toEqual(expectedVersion);

  const response = await page.goto('/login');
  expect(response?.status()).toBe(200);
  const policy = response?.headers()['content-security-policy'] ?? '';
  const directives = policy.split(';').map((value) => value.trim());
  for (const directive of [
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "img-src 'self'",
    `media-src 'self' ${world.store_origin}`,
    `connect-src 'self' ${world.store_origin}`,
    "frame-src 'none'",
    "object-src 'none'",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "require-trusted-types-for 'script'",
  ]) {
    expect(directives).toContain(directive);
  }
  expect(policy).not.toMatch(/unsafe-|\*/);
  expect(response?.headers()['content-security-policy-report-only']).toBeUndefined();
  await expect(page.locator('#vigia-app')).toBeVisible();
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
  // No dar por válida una página de relevo ni el aviso de navegador incompatible.
  await expect(page.getByLabel(/correo/i)).toBeVisible();
  await expect(page.getByLabel(/contraseña/i)).toBeVisible();
});
