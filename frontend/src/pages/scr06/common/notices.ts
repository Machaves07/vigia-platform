/**
 * Avisos de la consola de comisionamiento (UI-19, BR-APP-71 a 78): el error de un puerto se
 * presenta con el comportamiento del mapa cerrado y, si el servidor lo ancla a un campo
 * (`invalid_request` o `conflict`), junto a ese campo.
 *
 * La pantalla no traduce códigos de negocio: el mensaje sale del mapa de `ports.errors` y de las
 * etiquetas de `labels.platform.es.json` (`catalog_*`), que la pila ya resuelve. Aquí solo se
 * construye el aviso desde lo que el puerto devuelve, sin inventar mensajes.
 *
 * Nota de alcance: el armazón (`app.errors`, LC-APP-04) aún no existe en `main`; mientras llega,
 * cada pantalla construye sus avisos con este módulo.
 */
import type { Notice } from '@/components/NoticeStack/notice';
import { isPortError } from '@/ports/shared';

/**
 * El aviso de `error`, o `null` si no hay nada que avisar (una lectura cancelada por el cambio de
 * pantalla no es un fallo del usuario, BR-APP-71).
 */
export function noticeFromError(
  error: unknown,
  id: string,
  field: string | null = null,
): Notice | null {
  if (isPortError(error)) {
    const { failure } = error;
    switch (failure.kind) {
      case 'http':
        return { id, error: failure.error, field };
      case 'invalid_input':
        // El espejo del esquema ya lo comprueba antes de enviar; si aun así llega, es del cuerpo.
        return { id, error: { code: 'invalid_request', status: 422 }, field };
      case 'unavailable':
        return { id, error: { code: 'not_found', status: 404 }, field };
      case 'offline':
      case 'timeout':
      case 'network':
        return { id, error: { code: 'temporarily_unavailable', status: 0 }, field };
      case 'aborted':
        return null;
    }
  }
  return { id, error: { code: 'internal_error', status: 0 }, field };
}
