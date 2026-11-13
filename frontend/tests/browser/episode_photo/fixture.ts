/**
 * Datos sintéticos compartidos por el arnés del navegador (`harness.tsx`, `control.ts`) y la
 * prueba `episode_photo_csp.spec.ts`: el episodio y la concesión con que el servidor de la prueba
 * responde como la plataforma (la imagen solo con `X-Vigia-Concession`; sin ella, `not_found`).
 * Ningún dato real: la imagen es el JPEG sintético que exporta el backend.
 */
export const PHOTO_ID = '0196a0a0-0000-7000-8000-00000000f001';
/** Concesión sintética del arnés (la prueba no tiene sesión ni cadena: solo la cabecera). */
export const CONCESSION = '0196a0a0-0000-7000-8000-00000000c001';
export const IMAGE_PATH = `/episode-photos/${PHOTO_ID}/image`;
export const ALT = 'Foto difuminada sintética del episodio';
