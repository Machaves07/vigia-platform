/**
 * Composición del enlace de la vista en vivo (PAT-APP-SEG-03, BR-APP-65, D-1, PR-APP-20).
 *
 * El token viaja **solo en el fragmento** de la URL (`#token=…`): el fragmento no se envía al
 * servidor ni queda en los registros de acceso, y la página del nodo lo lee, lo borra del historial
 * y lo canjea por su cookie de sesión local (D-1). Nunca va en la ruta, la consulta, una cabecera,
 * una cookie, el almacenamiento, un registro ni el historial de la aplicación.
 *
 * La URL base es la que anunció el nodo (`live_view_local_url`, pendiente nº 31 y A-35): aquí se
 * reduce a su origen con ruta `/`, sin credenciales, consulta ni fragmento, de modo que ningún
 * valor ajeno pueda arrastrar nada junto al token. Si el nodo todavía no la anunció, o si el valor
 * recibido no es utilizable, no hay enlace: la pantalla lo explica y no ofrece nada que pulsar.
 */

/** Cómo se abre la vista en vivo: pestaña nueva, sin `opener` y sin referente (BR-APP-65). */
export const LIVE_VIEW_LINK_TARGET = '_blank';
export const LIVE_VIEW_LINK_REL = 'noopener noreferrer';

/** Límites del contrato: `live_view_local_url` ≤ 256 caracteres; el token, una JWS compacta. */
const MAX_LOCAL_URL_LENGTH = 256;
const MAX_TOKEN_LENGTH = 512;
const LOCAL_URL_SCHEME = 'https:';
/** Token del nodo: JWS compacta (segmentos `base64url` separados por puntos). */
const TOKEN = /^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*$/;

/** Por qué no se pudo componer el enlace. */
export type LiveViewLinkProblem =
  /** El nodo no ha anunciado su dirección (pendiente nº 31): no hay enlace que ofrecer. */
  | 'no_local_url'
  /** El valor recibido no es una dirección local utilizable: se falla cerrado, sin enlace. */
  | 'unusable';

export type LiveViewLink =
  | { readonly kind: 'link'; readonly href: string }
  | { readonly kind: 'no_local_url' }
  | { readonly kind: 'unusable' };

/** El token, solo si tiene la forma del contrato; cualquier otra cosa se descarta. */
function tokenOf(value: unknown): string | null {
  return typeof value === 'string' &&
    value.length > 0 &&
    value.length <= MAX_TOKEN_LENGTH &&
    TOKEN.test(value)
    ? value
    : null;
}

/**
 * Origen del nodo normalizado (esquema `https`, la dirección anunciada y la ruta `/`), o `null` si
 * el valor no sirve: no es texto, está vacío, supera el límite, no es `https` o trae credenciales.
 */
function liveViewBaseUrl(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const candidate = value.trim();
  if (candidate.length === 0 || candidate.length > MAX_LOCAL_URL_LENGTH) return null;
  let url: URL;
  try {
    url = new URL(candidate);
  } catch {
    return null;
  }
  if (url.protocol !== LOCAL_URL_SCHEME) return null;
  if (url.hostname.length === 0 || url.username.length > 0 || url.password.length > 0) return null;
  return `${url.origin}/`;
}

/**
 * Enlace de apertura (la dirección anunciada por el nodo con el token en el fragmento,
 * `<live_view_local_url>#token=<token>`) o el motivo por el que no hay ninguno. El fragmento se escribe en claro porque el token es `base64url`; un token con otra
 * forma no se coloca en el DOM.
 */
export function liveViewLink(handoff: {
  readonly token: unknown;
  readonly live_view_local_url: unknown;
}): LiveViewLink {
  if (
    handoff.live_view_local_url === null ||
    handoff.live_view_local_url === undefined ||
    handoff.live_view_local_url === ''
  ) {
    return { kind: 'no_local_url' };
  }
  const base = liveViewBaseUrl(handoff.live_view_local_url);
  const token = tokenOf(handoff.token);
  if (base === null || token === null) return { kind: 'unusable' };
  return { kind: 'link', href: `${base}#token=${token}` };
}
