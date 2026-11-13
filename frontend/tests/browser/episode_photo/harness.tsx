/**
 * Arnés de `episode_photo_csp.spec.ts` (TASK-574): monta UI-26 `BlurredEpisodePhoto` tal cual,
 * con el transporte real de `ports.base` y una concesión instalada, en una página servida con las
 * cabeceras de seguridad del backend. Nada se simula en el componente: la imagen sale por `fetch`
 * con `X-Vigia-Concession` y se pinta con `createImageBitmap` en el lienzo.
 */
import { createRoot } from 'react-dom/client';

import { BlurredEpisodePhoto } from '@/components/BlurredEpisodePhoto/BlurredEpisodePhoto';
import { createPortConnection, createTransport, installTransport } from '@/ports/base';
import { systemClock } from '@/state/clock';

import { ALT, CONCESSION, PHOTO_ID } from './fixture';

installTransport(
  createTransport({
    clock: systemClock,
    connection: createPortConnection(systemClock),
    concession: () => CONCESSION,
    devLog: () => undefined,
  }),
);

const root = document.getElementById('root');
if (root === null) throw new Error('Falta #root en el arnés');
createRoot(root).render(
  <BlurredEpisodePhoto photoId={PHOTO_ID} hasImage absentReason={null} alt={ALT} />,
);
