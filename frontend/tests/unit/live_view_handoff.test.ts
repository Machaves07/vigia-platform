import { describe, expect, it } from 'vitest';

import { liveViewLink } from '@/ports/liveview';

/** Reclamos completos de shared.tokens.LiveViewTokenService, no un token abreviado del doble. */
function fullToken(): string {
  const id = '0190a1b2-0000-7000-8000-000000000001';
  const encode = (value: unknown) => Buffer.from(JSON.stringify(value)).toString('base64url');
  return [
    encode({ alg: 'EdDSA', typ: 'JWT', kid: id }),
    encode({
      jti: id,
      iss: 'vigia-platform',
      aud: id,
      sub: id,
      organization_id: id,
      plant_id: id,
      zone_id: id,
      role: 'provider_installer',
      iat: 1_800_000_000,
      exp: 1_800_000_600,
      purpose: 'live_view',
    }),
    Buffer.alloc(64).toString('base64url'),
  ].join('.');
}

describe('IF-19: entrega con reclamos completos de la plataforma', () => {
  it('acepta la longitud real de una JWS EdDSA con los seis UUID y el rol del instalador', () => {
    const token = fullToken();
    expect(token.length).toBeGreaterThan(512);
    expect(
      liveViewLink({ token, live_view_local_url: 'https://node.example.test:8443/' }).kind,
    ).toBe('link');
  });

  it('conserva un límite de transporte cerrado en 2048 caracteres', () => {
    const live_view_local_url = 'https://node.example.test:8443/';
    expect(liveViewLink({ token: 'a'.repeat(2048), live_view_local_url }).kind).toBe('link');
    expect(liveViewLink({ token: 'a'.repeat(2049), live_view_local_url }).kind).toBe('unusable');
  });
});
