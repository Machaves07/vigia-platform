/** URLs efímeras fuera de la caché: se emiten únicamente al pulsar. */
import { useEffect, useState } from 'react';
import { FieldNotice } from '@/components/NoticeStack/NoticeStack';
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import {
  conflictState,
  downloadHref,
  exportNotice,
  exportsClock,
  exportsPort,
  type DocumentDownload,
  type ExportState,
  type VolumeDownloads,
} from '@/ports/scr14Exports';
import { useScreenMutation } from '@/state/query';
import { text } from '@/texts';
import { Result } from './shared';

function DownloadLink({
  url,
  expiresAt,
  caption,
  timeZone,
}: {
  readonly url: string;
  readonly expiresAt: string;
  readonly caption: string;
  readonly timeZone: string;
}) {
  const clock = exportsClock();
  const [, tick] = useState(0);
  useEffect(() => {
    const handle = clock.setTimeout(
      () => {
        tick((value) => value + 1);
      },
      Math.max(0, Date.parse(expiresAt) - clock.now()),
    );
    return () => {
      clock.clearTimeout(handle);
    };
  }, [clock, expiresAt]);
  const href = downloadHref(url);
  const expired = !Number.isFinite(Date.parse(expiresAt)) || clock.now() >= Date.parse(expiresAt);
  return (
    <p data-download-expired={expired}>
      {expired || href === undefined ? (
        text('scr14.link_expired')
      ) : (
        <a
          href={href}
          target="_blank"
          rel="noopener noreferrer"
          onClick={(event) => {
            if (clock.now() >= Date.parse(expiresAt)) event.preventDefault();
          }}
        >
          {caption}
        </a>
      )}{' '}
      {text('scr14.expires')}: <TimestampWithOffset instant={expiresAt} timeZone={timeZone} />
    </p>
  );
}

export function Downloads({
  exportId,
  timeZone,
  onConflict,
}: {
  readonly exportId: string;
  readonly timeZone: string;
  readonly onConflict: (state: ExportState) => void;
}) {
  const [document, setDocument] = useState<DocumentDownload | null>(null);
  const [volumes, setVolumes] = useState<VolumeDownloads | null>(null);
  const mutation = useScreenMutation({
    write: async (input: 'document' | 'volumes') => {
      const path = { export_id: exportId };
      if (input === 'document') setDocument(await exportsPort.document.write({ path }));
      else setVolumes(await exportsPort.volumes.read({ path }));
    },
    invalidates: { screen: 'scr14', resources: ['exports'] },
  });
  const run = (input: 'document' | 'volumes') => {
    setDocument(null);
    setVolumes(null);
    void mutation.run(input).catch((error: unknown) => {
      const state = conflictState(error instanceof Error ? error : null);
      if (state !== null) onConflict(state);
    });
  };
  const conflict = conflictState(mutation.error);
  const notice = conflict === null ? exportNotice(mutation.error) : null;
  const storage = notice?.error.code === 'storage_unavailable';
  return (
    <section aria-label={text('scr14.volumes')}>
      <p>{text('scr14.link_lifetime')}</p>
      <button
        type="button"
        disabled={mutation.status === 'pending'}
        onClick={() => {
          run('document');
        }}
      >
        {text('scr14.document')}
      </button>
      <button
        type="button"
        disabled={mutation.status === 'pending'}
        onClick={() => {
          run('volumes');
        }}
      >
        {text('scr14.volumes')}
      </button>
      {storage && <p role="status">{text('scr14.storage_unavailable')}</p>}
      {notice !== null && <FieldNotice id="scr14-download-notice" notice={notice} />}
      {mutation.status === 'pending' && (
        <Result query={{ status: mutation.status, error: null, isFetching: true }}>{null}</Result>
      )}
      {mutation.error !== null && notice === null && conflict === null && (
        <Result query={{ status: mutation.status, error: mutation.error, isFetching: false }}>
          {null}
        </Result>
      )}
      {document !== null && (
        <>
          <p>
            {text('scr14.document_hash')}: <code>{document.document_sha256}</code>
          </p>
          <DownloadLink
            url={document.url}
            expiresAt={document.expires_at}
            timeZone={timeZone}
            caption={text('scr14.download_document')}
          />
        </>
      )}
      {volumes !== null && (
        <>
          <p>
            {text('scr14.archive_hash')}: <code>{volumes.archive_sha256}</code>
          </p>
          {volumes.volumes.map((volume) => (
            <div key={volume.volume}>
              <p>
                {volume.name}: <code>{volume.sha256}</code>
              </p>
              <DownloadLink
                url={volume.url}
                expiresAt={volume.expires_at}
                timeZone={timeZone}
                caption={`${text('scr14.download_volume')} ${String(volume.volume)}`}
              />
            </div>
          ))}
        </>
      )}
    </section>
  );
}
