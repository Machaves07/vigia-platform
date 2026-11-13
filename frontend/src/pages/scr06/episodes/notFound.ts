/**
 * `not_found` de la API (una zona en `productive`, una foto vencida o purgada, otra planta, una
 * concesión vencida…): la pantalla entera pasa a «no disponible en su alcance» (BR-APP-77), sin
 * distinguir «no existe» de «no es suyo». Lo demás va a la pila de avisos (UI-19).
 */
import { isPortError } from '@/ports/shared';

export function isNotFound(error: unknown): boolean {
  return (
    isPortError(error) && error.failure.kind === 'http' && error.failure.error.code === 'not_found'
  );
}
