/** Retiro y unipersonal siempre publican una versión, con motivo obligatorio. */
import { useId, useState } from 'react';
import { ReasonField, reasonIssueKey } from '@/components/ReasonField/ReasonField';
import { FieldNotice, NoticeStack } from '@/components/NoticeStack/NoticeStack';
import { noticeForField } from '@/components/NoticeStack/notice';
import {
  CATALOG_REASON_LIMITS as LIMITS,
  catalogClock,
  catalogNotice,
  catalogPort,
  type CatalogVersion,
} from '@/ports/scr04Catalog';
import { useScreenMutation } from '@/state/query';
import { text } from '@/texts';

export function Changes({
  zoneId,
  version,
  standardId,
}: {
  readonly zoneId: string;
  readonly version: CatalogVersion;
  readonly standardId?: string;
}) {
  const id = useId();
  const retirement = standardId !== undefined;
  const [reason, setReason] = useState('');
  const [showErrors, setShowErrors] = useState(false);
  const [occupancy, setOccupancy] = useState(version.single_occupancy);
  const [window, setWindow] = useState(version.aggregation_window_minutes);
  const [date, setDate] = useState('');
  const mutation = useScreenMutation({
    write: async () =>
      retirement
        ? catalogPort.retire.write({
            path: { zone_id: zoneId, standard_id: standardId },
            body: { effective_from: `${date}:00.000Z`, reason_es: reason.normalize('NFC') },
          })
        : catalogPort.occupancy.write({
            path: { zone_id: zoneId },
            body: {
              single_occupancy: occupancy,
              aggregation_window_minutes: window,
              reason_es: reason.normalize('NFC'),
            },
          }),
    invalidates: {
      screen: 'scr04',
      resources: retirement ? ['catalog', 'regression'] : ['catalog'],
    },
  });
  const notice = catalogNotice(mutation.error);
  const notices = notice === null ? [] : [notice];
  const dateNotice = noticeForField(notices, 'effective_from');
  const formNotice = noticeForField(notices, 'form');
  return (
    <form
      aria-label={text(retirement ? 'scr04.catalog.retire' : 'scr04.catalog.single_occupancy')}
      onSubmit={(event) => {
        event.preventDefault();
        setShowErrors(true);
        if (
          reasonIssueKey(reason, LIMITS) !== null ||
          (!retirement && (!Number.isInteger(window) || window < 15 || window > 480)) ||
          (retirement && date === '')
        )
          return;
        void mutation.run(undefined).catch(() => undefined);
      }}
    >
      <h3>{text(retirement ? 'scr04.catalog.retire' : 'scr04.catalog.single_occupancy')}</h3>
      <NoticeStack
        notices={notices}
        clock={catalogClock()}
        onDismiss={() => {
          mutation.reset();
        }}
      />
      {formNotice === undefined ? null : (
        <FieldNotice id={`${id}-form-notice`} notice={formNotice} />
      )}
      {retirement ? (
        <>
          <label htmlFor={`${id}-date`}>{text('scr04.catalog.retirement_date')}</label>
          <input
            id={`${id}-date`}
            type="datetime-local"
            required
            value={date}
            onChange={(event) => {
              setDate(event.target.value);
            }}
            aria-invalid={dateNotice !== undefined}
            aria-describedby={dateNotice === undefined ? undefined : `${id}-date-notice`}
          />
          {dateNotice === undefined ? null : (
            <FieldNotice id={`${id}-date-notice`} notice={dateNotice} />
          )}
        </>
      ) : (
        <>
          <label>
            <input
              type="checkbox"
              checked={occupancy}
              onChange={(event) => {
                setOccupancy(event.target.checked);
              }}
            />
            {text('scr04.catalog.single_occupancy')}
          </label>
          <label htmlFor={`${id}-window`}>{text('scr04.catalog.window')}</label>
          <input
            id={`${id}-window`}
            type="number"
            min="15"
            max="480"
            step="1"
            required
            value={window}
            onChange={(event) => {
              setWindow(event.target.valueAsNumber);
            }}
          />
        </>
      )}
      <ReasonField
        label={text('scr04.catalog.reason')}
        value={reason}
        onChange={setReason}
        limits={LIMITS}
        showErrors={showErrors}
        notice={noticeForField(notices, 'reason_es')}
      />
      <button type="submit" disabled={mutation.status === 'pending'}>
        {text(retirement ? 'scr04.catalog.retire' : 'scr04.catalog.save_occupancy')}
      </button>
      {mutation.status === 'success' ? <p role="status">{text('scr04.catalog.saved')}</p> : null}
      {mutation.error !== null && notice === null ? (
        <p role="alert">{text('validation.invalid')}</p>
      ) : null}
    </form>
  );
}
