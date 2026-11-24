/**
 * Detalle de un episodio de la sombra (TASK-574; A-112, QF10; BR-APP-58, 77).
 *
 * - La API no tiene lectura de un solo episodio: sus metadatos se buscan en el listado de la zona
 *   (`findEpisodePhoto`, páginas de 200). Sin él, «no disponible en su alcance».
 * - La foto la pide y la pinta UI-26 (`BlurredEpisodePhoto`): una petición por apertura, sin
 *   reintentos ni recarga al enfocar, y ninguna si el episodio llegó sin imagen (se muestra el
 *   motivo etiquetado). El aviso de material de puesta en marcha está siempre; la superposición
 *   «Imagen difuminada automáticamente», siempre que hay imagen (sin foto no la hay; A-112).
 * - `not_found` del listado o de la imagen (zona en `productive`, foto vencida o purgada, fuera de
 *   alcance): la pantalla entera pasa a «no disponible en su alcance». Lo transitorio va a la pila
 *   de avisos (UI-19); volver a buscar o a pedir la imagen es siempre un gesto de la persona.
 * - La foto y sus datos van en el panel compartido `EpisodePhotoPanel` (TASK-590), en modo
 *   `commissioning`: las mismas filas, textos y orden que antes de extraerlo.
 * - Bajo la foto, los valores de proceso del episodio (TASK-596, A-129 Q6), del mismo listado:
 *   sin bloque, nada. Las horas, tal como las da el servidor.
 */
import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router';

import { BlurredEpisodePhoto } from '@/components/BlurredEpisodePhoto/BlurredEpisodePhoto';
import { EpisodePhotoPanel } from '@/components/EpisodePhotoPanel/EpisodePhotoPanel';
import { NoticeStack } from '@/components/NoticeStack/NoticeStack';
import { useScreenNotices } from '@/pages/scr06/common/useScreenNotices';
import { hasProcessValues, ProcessValues } from '@/pages/shared/ProcessValues';
import { NotAvailableInScope, ScreenState } from '@/pages/shared/ScreenState';
import { isPortError } from '@/ports/shared';
import { findEpisodePhoto, type EpisodePhoto } from '@/ports/scr06EpisodePhotos';
import type { Clock } from '@/state/clock';
import { text } from '@/texts';

import { durationMsOf, serverInstant } from './format';
import { isNotFound } from './notFound';
import { episodesPath } from './paths';
import styles from './episodes.module.css';

export interface EpisodeDetailScreenProps {
  readonly zoneId: string;
  readonly photoId: string;
  readonly clock: Clock;
  /** Búsqueda del episodio en el listado; por omisión, la del puerto. */
  readonly find?: (
    zoneId: string,
    photoId: string,
    signal: AbortSignal,
  ) => Promise<EpisodePhoto | null>;
}

type Lookup =
  | { readonly kind: 'loading' }
  | { readonly kind: 'found'; readonly photo: EpisodePhoto }
  | { readonly kind: 'not_found' }
  | { readonly kind: 'failed' };

export function EpisodeDetailScreen({
  zoneId,
  photoId,
  clock,
  find = findEpisodePhoto,
}: EpisodeDetailScreenProps) {
  const notices = useScreenNotices();
  const { add } = notices;
  const [lookup, setLookup] = useState<Lookup>({ kind: 'loading' });
  const [search, setSearch] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    find(zoneId, photoId, controller.signal).then(
      (photo) => {
        setLookup(photo === null ? { kind: 'not_found' } : { kind: 'found', photo });
      },
      (error: unknown) => {
        if (controller.signal.aborted) return;
        if (isPortError(error) && error.failure.kind === 'aborted') return;
        if (isNotFound(error)) {
          setLookup({ kind: 'not_found' });
          return;
        }
        add(error);
        setLookup({ kind: 'failed' });
      },
    );
    return () => {
      controller.abort();
    };
  }, [find, zoneId, photoId, search, add]);

  const onImageError = useCallback(
    (error: unknown) => {
      if (isNotFound(error)) setLookup({ kind: 'not_found' });
      else add(error);
    },
    [add],
  );

  if (lookup.kind === 'not_found') return <NotAvailableInScope />;

  return (
    <main className={styles.page}>
      <h1 className={styles.title}>{text('scr06_episodes.detail.title')}</h1>
      <p>
        <Link to={episodesPath(zoneId)}>{text('scr06_episodes.detail.back')}</Link>
      </p>
      <NoticeStack
        notices={notices.notices}
        clock={clock}
        onDismiss={(id) => {
          notices.dismiss(id);
        }}
      />
      {lookup.kind === 'loading' ? (
        <ScreenState state="loading" />
      ) : lookup.kind === 'failed' ? (
        <ScreenState
          state="error"
          onRetry={() => {
            setLookup({ kind: 'loading' });
            setSearch((value) => value + 1);
          }}
        />
      ) : (
        <EpisodePhotoPanel
          mode="commissioning"
          photo={{ ...lookup.photo, has_image: lookup.photo.image !== null }}
          segment={lookup.photo.segment_index}
          durationMs={durationMsOf(lookup.photo)}
          instant={serverInstant}
          media={
            <BlurredEpisodePhoto
              photoId={lookup.photo.photo_id}
              hasImage={lookup.photo.image !== null}
              absentReason={lookup.photo.image_absent_reason}
              alt={text('scr06_episodes.detail.photo_alt')}
              notice="commissioning"
              onImageError={onImageError}
            />
          }
          below={
            hasProcessValues(lookup.photo.process_context) ? (
              <section aria-label={text('process_values.title')} data-part="episode_process_values">
                <h2>{text('process_values.title')}</h2>
                <ProcessValues
                  context={lookup.photo.process_context}
                  name={text('process_values.title')}
                  instant={serverInstant}
                  aggregated={false}
                />
              </section>
            ) : null
          }
        />
      )}
    </main>
  );
}
