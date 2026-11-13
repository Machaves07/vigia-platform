/**
 * Controles de `episode_photo_csp.spec.ts`: lo que harían las dos formas prohibidas por A-112,
 * para comprobar que la prueba las distinguiría con la política y el servidor reales.
 *
 * - `#blob`: la imagen pedida con la concesión y puesta como URL de objeto en un `<img>`; la CSP
 *   del backend (`img-src 'self'`) la bloquea (mutación `m_blob_url`).
 * - `#direct`: un `<img src>` directo, que no puede enviar `X-Vigia-Concession` y recibe
 *   `not_found` (mutación `m_img_src_directo`).
 */
import { CONCESSION, IMAGE_PATH } from './fixture';

const mode = location.hash.slice(1);
const image = document.createElement('img');
image.alt = 'Control sintético';
image.addEventListener('load', () => {
  document.body.dataset.result = 'load';
});
image.addEventListener('error', () => {
  document.body.dataset.result = 'error';
});

if (mode === 'blob') {
  const response = await fetch(IMAGE_PATH, {
    headers: { 'X-Vigia-Concession': CONCESSION },
    credentials: 'same-origin',
  });
  image.src = URL.createObjectURL(await response.blob());
} else {
  image.src = IMAGE_PATH;
}
document.body.append(image);
