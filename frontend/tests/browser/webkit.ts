/**
 * VIG-345: WebKit fijado para las pruebas de navegador bajo la CSP de producción.
 *
 * Desde la revisión 2370 (Playwright 1.64.x), WebKit informa `style-src-attr` al ejecutar
 * `document.createElement("select")` con `style-src 'self'`, sin estilos de la aplicación. La
 * revisión 2359 (WebKit 26.6, Playwright 1.63.0) no lo hace y sigue rechazando un estilo en
 * línea. No se relaja la CSP ni se filtran eventos: se cambia solo el ejecutable del motor.
 * `scripts/install_webkit.mjs` lo descarga con su SHA-256; si falta, Playwright falla al lanzar.
 */
import { homedir } from 'node:os';
import { join } from 'node:path';

import build from './webkit-build.json' with { type: 'json' };

/** Directorio propio, fuera de la caché de Playwright, que borra revisiones ajenas a su versión. */
function compatibleWebkitDir(env: NodeJS.ProcessEnv = process.env): string {
  const override = env.VIGIA_WEBKIT_DIR;
  return override !== undefined && override !== ''
    ? override
    : join(homedir(), '.cache', 'vigia', `webkit-${build.revision}`);
}

export function compatibleWebkitExecutable(env: NodeJS.ProcessEnv = process.env): string {
  return join(compatibleWebkitDir(env), 'pw_run.sh');
}
