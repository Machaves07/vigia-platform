/** Porciones antiguas: mismo bloqueo que una versión distinta, jamás location.reload automático. */
import { appVersion, type AppVersion } from './version';

/** Solo se envuelve la carga de código, no errores de render ni fallos de datos de la API. */
export async function guardChunkLoad<T>(
  load: () => Promise<T>,
  version: AppVersion = appVersion,
): Promise<T> {
  try {
    return await load();
  } catch (error) {
    version.markOutdated();
    void version.check();
    throw error;
  }
}

/** Vite emite este evento antes de rechazar un import dinámico (incluidos CSS con hash). */
export function installChunkLoadGuard(
  target: Window = window,
  version: AppVersion = appVersion,
): () => void {
  const onError = () => {
    version.markOutdated();
    void version.check();
  };
  target.addEventListener('vite:preloadError', onError);
  return () => {
    target.removeEventListener('vite:preloadError', onError);
  };
}
