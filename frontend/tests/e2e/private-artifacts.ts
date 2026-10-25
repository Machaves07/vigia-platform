import type { PlaywrightWorkerOptions } from '@playwright/test';

/** También cubre sobreescrituras de test.use: nunca grabar contraseña, OTP o cookies de sesión. */
export function assertPrivateArtifacts(
  options: Pick<PlaywrightWorkerOptions, 'trace' | 'video' | 'screenshot'>,
): void {
  if (options.trace !== 'off' || options.video !== 'off' || options.screenshot !== 'off') {
    throw new Error('La autenticación exige trazas, vídeo y capturas desactivados');
  }
}
