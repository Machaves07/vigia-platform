import { createHmac } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';

import { expect } from '@playwright/test';

import { systemClock } from '../../src/state/clock';

export function totpAt(secret: string, step: number): string {
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(step));
  const digest = createHmac('sha1', Buffer.from(secret, 'hex')).update(counter).digest();
  const offset = (digest.at(-1) ?? 0) & 15;
  return String((digest.readUInt32BE(offset) & 0x7fffffff) % 1_000_000).padStart(6, '0');
}

/** El estado fuera del árbol coordina los proyectos seriales sin reutilizar un paso TOTP. */
export async function nextTotp(
  secret: string,
  role: string,
  credentialsFile: string,
): Promise<string> {
  const statePath = `${credentialsFile}.steps`;
  const steps = JSON.parse(readFileSync(statePath, 'utf8')) as Record<string, number>;
  const previous = steps[role] ?? -1;
  await expect
    .poll(() => Math.floor(systemClock.now() / 30_000) > previous, {
      timeout: 65_000,
      message: 'Esperar un paso TOTP aún no utilizado',
    })
    .toBe(true);
  const step = Math.floor(systemClock.now() / 30_000);
  steps[role] = step;
  writeFileSync(statePath, JSON.stringify(steps), { mode: 0o600 });
  return totpAt(secret, step);
}
