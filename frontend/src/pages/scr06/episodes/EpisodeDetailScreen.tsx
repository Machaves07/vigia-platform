/**
 * Detalle de un episodio de la sombra (TASK-574; A-112, QF10; BR-APP-58, 77).
 *
 * - La API no tiene lectura de un solo episodio: sus metadatos se buscan en el listado de la zona
 *   (`findEpisodePhoto`, páginas de 200). Sin él, «no disponible en su alcance».
 * - La foto la pide y la pinta UI-26 (`BlurredEpisodePhoto`): una petición por apertura, sin
 *   reintentos ni recarga al enfocar, y ninguna si el episodio llegó sin imagen (se muestra el
 *   motivo etiquetado). El aviso de material de puesta en marcha y la superposición «Imagen
 *   difuminada automáticamente» están siempre.
 * - `not_found` del listado o de la imagen (zona en `productive`, foto vencida o purgada, fuera de
 *   alcance): la pantalla entera pasa a «no disponible en su alcance». Lo transitorio va a la pila
 *   de avisos (UI-19); volver a buscar o a pedir la imagen es siempre un gesto de la persona.
 */
import { useCallback, useEffect, useState, type ReactNode } from 'react';
import { Link } from 'react-router';

import { BlurredEpisodePhoto } from '@/components/BlurredEpisodePhoto/BlurredEpisodePhoto';
import { NoticeStack } from '@/components/NoticeStack/NoticeStack';
import { ServerTimestamp } from '@/pages/scr06/common/ServerTimestamp';
import { useScreenNotices } from '@/pages/scr06/common/useScreenNotices';
import { NotAvailableInScope, ScreenState } from '@/pages/shared/ScreenState';
import { isPortError } from '@/ports/shared';
import { findEpisodePhoto, type EpisodePhoto } from '@/ports/scr06EpisodePhotos';
import type { Clock } from '@/state/clock';
import { text } from '@/texts';

import {
  cameraOf,
  confidenceOf,
  durationOf,
  familyOf,
  FrameTime,
  PhotoState,
  standardOf,
  wouldHaveBeen,
} from './format';
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

function Facts({ photo }: { readonly photo: EpisodePhoto }) {
  const rows: readonly (readonly [string, ReactNode])[] = [
    [text('scr06_episodes.col.frame_at'), <FrameTime key="frame" photo={photo} />],
    [text('scr06_episodes.col.camera'), cameraOf(photo)],
    [text('scr06_episodes.col.would_have_been'), wouldHaveBeen(photo)],
    [text('scr06_episodes.col.family'), familyOf(photo)],
    [text('scr06_episodes.col.standard'), standardOf(photo)],
    [text('scr06_episodes.col.max_confidence'), confidenceOf(photo)],
    [text('scr06_episodes.detail.segment'), String(photo.segment_index)],
    [
      text('scr06_episodes.detail.started_at'),
      <ServerTimestamp key="start" instant={photo.started_at} />,
    ],
    [
      text('scr06_episodes.detail.ended_at'),
      <ServerTimestamp key="end" instant={photo.ended_at} />,
    ],
    [text('scr06_episodes.col.duration'), durationOf(photo)],
    [
      text('scr06_episodes.detail.received_at'),
      <ServerTimestamp key="received" instant={photo.received_at} />,
    ],
    [
      text('scr06_episodes.col.expires_at'),
      <ServerTimestamp key="expires" instant={photo.expires_at} />,
    ],
    [text('scr06_episodes.col.photo'), <PhotoState key="photo" photo={photo} />],
  ];
  return (
    <section className={styles.panel} aria-label={text('scr06_episodes.detail.facts')}>
      <dl className={styles.facts}>
        {rows.map(([name, value]) => (
          <div key={name} className={styles.fact}>
            <dt>{name}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

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
        <div className={styles.detail}>
          <BlurredEpisodePhoto
            photoId={lookup.photo.photo_id}
            hasImage={lookup.photo.image !== null}
            absentReason={lookup.photo.image_absent_reason}
            alt={text('scr06_episodes.detail.photo_alt')}
            onImageError={onImageError}
          />
          <Facts photo={lookup.photo} />
        </div>
      )}
    </main>
  );
}
