import { createHmac } from 'node:crypto';
import { readFileSync } from 'node:fs';

import { test, expect } from '@playwright/test';

import { systemClock } from '../../src/state/clock';

import { watchPolicy } from './policy';

interface DeployCredentials {
  readonly invitation_link: string;
  readonly email: string;
}

let assertPolicy: (() => void) | undefined;

test.beforeEach(async ({ page }) => {
  assertPolicy = await watchPolicy(page.context(), 'recorrido de despliegue');
});

test.afterEach(() => {
  assertPolicy?.();
  assertPolicy = undefined;
});

function credentials(): DeployCredentials {
  const path = process.env.VIGIA_DEPLOY_CHECK_CREDENTIALS_FILE;
  if (!path) throw new Error('Falta el archivo privado de credenciales de despliegue');
  return JSON.parse(readFileSync(path, 'utf8')) as DeployCredentials;
}

function totp(provisioningUri: string): string {
  const encoded = new URL(provisioningUri).searchParams.get('secret');
  if (!encoded) throw new Error('La inscripción no entregó un secreto TOTP');
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
  let bits = '';
  for (const character of encoded.toUpperCase().replace(/=/g, '')) {
    const value = alphabet.indexOf(character);
    if (value < 0) throw new Error('El secreto TOTP no está en base32');
    bits += value.toString(2).padStart(5, '0');
  }
  const secret = Buffer.from(bits.match(/.{8}/g)?.map((octet) => Number.parseInt(octet, 2)) ?? []);
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(Math.floor(systemClock.now() / 30_000)));
  const digest = createHmac('sha1', secret).update(counter).digest();
  const offset = digest.at(-1) ?? 0;
  return String((digest.readUInt32BE(offset & 15) & 0x7fffffff) % 1_000_000).padStart(6, '0');
}

test('el despliegue activa la invitación y recorre el panel sin violaciones de política', async ({
  page,
}) => {
  const privateInput = credentials();
  const token = new URL(privateInput.invitation_link).hash.slice(1);
  if (!token) throw new Error('El enlace de invitación no contiene un token');
  const begin = page.waitForResponse(
    (response) =>
      response.url().includes(`/invitations/${token}/accept`) &&
      response.request().method() === 'POST',
  );
  await page.goto(`/invitations/${token}`);
  const response = await begin;
  const enrollment = ((await response.json()) as { enrollment?: { provisioning_uri?: string } })
    .enrollment;
  const provisioningUri = enrollment?.provisioning_uri;
  if (!provisioningUri) throw new Error('La invitación no pidió segundo factor');

  const password = 'Vigia-deploy-check-2026';
  await page.getByLabel('Contraseña nueva').fill(password);
  await page.getByLabel('Repita la contraseña').fill(password);
  await page.getByRole('button', { name: 'Siguiente' }).click();
  await page.getByRole('button', { name: 'Lo guardé' }).click();
  await page.getByLabel('Código de 6 dígitos de la aplicación').fill(totp(provisioningUri));
  await page.getByRole('button', { name: 'Siguiente' }).click();
  await page.getByLabel('He leído el aviso de tratamiento de datos y lo acepto').check();
  await page.getByRole('button', { name: 'Siguiente' }).click();
  await page.getByRole('button', { name: 'Enviar' }).click();
  await expect(page.getByText('Cuenta activada. Ya puede iniciar sesión.')).toBeVisible();

  await page.getByRole('link', { name: 'Ir al inicio de sesión' }).click();
  await page.getByLabel('Correo').fill(privateInput.email);
  await page.getByLabel('Contraseña').fill(password);
  await page.getByRole('button', { name: 'Entrar' }).click();
  await page.getByLabel('Código de 6 dígitos o código de recuperación').fill(totp(provisioningUri));
  await page.getByRole('button', { name: 'Verificar' }).click();
  await expect(page.getByRole('heading', { name: /Inicio/ })).toBeVisible();
  await page
    .getByRole('link', { name: /bandeja/i })
    .first()
    .click();
  await expect(page.getByRole('heading', { name: /Bandeja/ })).toBeVisible();
  await page.getByRole('button', { name: 'Cuenta', exact: true }).click();
  await page.getByRole('button', { name: 'Cerrar sesión', exact: true }).click();
  await expect(page).toHaveURL(/\/login$/);
});
