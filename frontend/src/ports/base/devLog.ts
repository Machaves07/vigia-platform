/**
 * Registro de desarrollo de `ports.base` (NFR-APP-42, SECURITY-03).
 *
 * Solo con `DEV` de `import.meta.env` (la construcción de producción lo sustituye por `false` y
 * la rama desaparece): una línea por intento con método, plantilla de ruta (`/zones/{zone_id}`,
 * nunca la ruta con identificadores), código y duración. La entrada no admite cuerpos, cabeceras
 * ni consulta: no hay campo donde ponerlos. Es el único archivo con `console` (ESLint).
 */
export interface DevLogEntry {
  readonly method: string;
  /** Plantilla de la especificación, sin valores de parámetros. */
  readonly route: string;
  /** Estado HTTP, o `null` sin respuesta (tiempo de espera, red, cancelación). */
  readonly status: number | null;
  /** `code` de la API o `timeout`, `network`, `aborted`. */
  readonly code: string;
  readonly durationMs: number;
}

export type DevLog = (entry: DevLogEntry) => void;

export interface DevLogSink {
  debug(message: string): void;
}

const SAFE_CODE = /^[a-z][a-z0-9_]{0,63}$/;

function formatDevLog(entry: DevLogEntry): string {
  const code = SAFE_CODE.test(entry.code) ? entry.code : 'other';
  const status = entry.status === null ? '---' : String(entry.status);
  return `[api] ${entry.method.toUpperCase()} ${entry.route} ${status} ${code} ${String(Math.round(entry.durationMs))} ms`;
}

/** `isDev` se evalúa en cada entrada: así una prueba puede simular producción. */
export function createDevLog(
  sink: DevLogSink = console,
  isDev: () => boolean = () => import.meta.env.DEV,
): DevLog {
  return (entry) => {
    if (!isDev()) return;
    sink.debug(formatDevLog(entry));
  };
}
