/**
 * Listado de los episodios de la sombra de la zona (TASK-574; A-112, QF10): UI-14 por clave, el
 * más reciente primero, con «actualizar» a demanda (UI-20). Solo metadatos: el listado no pide
 * ninguna imagen; la foto se ve en el detalle de cada episodio.
 *
 * - Columnas: hora del cuadro (UI-25), cámara, «habría sido» y familia con las etiquetas del
 *   contrato, versión del estándar, confianza máxima, duración, vencimiento y la foto (o «sin
 *   foto» con su motivo etiquetado).
 * - `not_found` (zona en `productive` o fuera de alcance): «no disponible en su alcance»
 *   (BR-APP-77). Un fallo transitorio va a la pila de avisos (UI-19) y la tabla lo dice.
 */
import { useCallback, useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router';

import { KeysetTable, type KeysetColumn } from '@/components/KeysetTable/KeysetTable';
import type { KeysetRequest } from '@/components/KeysetTable/keyset';
import { NoticeStack } from '@/components/NoticeStack/NoticeStack';
import { ServerTimestamp } from '@/pages/scr06/common/ServerTimestamp';
import { useScreenNotices } from '@/pages/scr06/common/useScreenNotices';
import { NotAvailableInScope } from '@/pages/shared/ScreenState';
import { readEpisodePhotos, type EpisodePhoto } from '@/ports/scr06EpisodePhotos';
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
import { episodePath, sessionPath } from './paths';
import styles from './episodes.module.css';

export interface EpisodesScreenProps {
  readonly zoneId: string;
  readonly clock: Clock;
}

function columns(zoneId: string): readonly KeysetColumn<EpisodePhoto>[] {
  return [
    {
      id: 'frame_at',
      header: text('scr06_episodes.col.frame_at'),
      cell: (photo) => <FrameTime photo={photo} />,
    },
    { id: 'camera', header: text('scr06_episodes.col.camera'), cell: cameraOf },
    {
      id: 'would_have_been',
      header: text('scr06_episodes.col.would_have_been'),
      cell: wouldHaveBeen,
    },
    { id: 'family', header: text('scr06_episodes.col.family'), cell: familyOf },
    { id: 'standard', header: text('scr06_episodes.col.standard'), cell: standardOf },
    {
      id: 'max_confidence',
      header: text('scr06_episodes.col.max_confidence'),
      cell: confidenceOf,
    },
    { id: 'duration', header: text('scr06_episodes.col.duration'), cell: durationOf },
    {
      id: 'expires_at',
      header: text('scr06_episodes.col.expires_at'),
      cell: (photo) => <ServerTimestamp instant={photo.expires_at} />,
    },
    {
      id: 'photo',
      header: text('scr06_episodes.col.photo'),
      cell: (photo) => <PhotoState photo={photo} />,
    },
    {
      id: 'open',
      header: text('scr06_episodes.col.open'),
      cell: (photo) => (
        <Link to={episodePath(zoneId, photo.photo_id)} data-part="open_episode">
          {text('scr06_episodes.open')}
        </Link>
      ),
    },
  ];
}

export function EpisodesScreen({ zoneId, clock }: EpisodesScreenProps) {
  const notices = useScreenNotices();
  const { add } = notices;
  const navigate = useNavigate();
  const [hidden, setHidden] = useState(false);
  const tableColumns = useMemo(() => columns(zoneId), [zoneId]);

  const fetchPage = useCallback(
    async (request: KeysetRequest, signal: AbortSignal) => {
      try {
        return await readEpisodePhotos(zoneId, request.cursor, request.pageSize, signal);
      } catch (error) {
        if (isNotFound(error)) setHidden(true);
        else add(error);
        throw error;
      }
    },
    [zoneId, add],
  );

  if (hidden) return <NotAvailableInScope />;

  return (
    <main className={styles.page}>
      <h1 className={styles.title}>{text('navigation.menu.episodes')}</h1>
      <p className={styles.intro}>{text('scr06_episodes.list.intro')}</p>
      <p>
        <Link to={sessionPath(zoneId)}>{text('scr06_episodes.list.back')}</Link>
      </p>
      <NoticeStack
        notices={notices.notices}
        clock={clock}
        onDismiss={(id) => {
          notices.dismiss(id);
        }}
      />
      <KeysetTable<EpisodePhoto>
        caption={text('scr06_episodes.list.caption')}
        columns={tableColumns}
        rowKey={(photo) => photo.photo_id}
        fetchPage={fetchPage}
        clock={clock}
        refreshPolicy="on_demand"
        emptyText={text('scr06_episodes.list.empty')}
        onRowActivate={(photo) => {
          void navigate(episodePath(zoneId, photo.photo_id));
        }}
        cardsOnTablet
      />
    </main>
  );
}
