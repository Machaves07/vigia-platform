/**
 * SCR-08, grupo `labels` (`labels.read`; `GET /labels`): etiquetas por zona, familia y resultado,
 * a demanda. Es una lectura auditada: nada se pide al abrir la pestaña, solo al pulsar
 * «Consultar»; nunca se refresca sola. Sin imágenes ni texto libre: cada fila muestra zona,
 * familia, salida, resultado, fecha y rol, nunca las evidencias ni quién etiquetó.
 */
import { useState, type SubmitEvent } from 'react';
import { Link } from 'react-router';
import { KeysetTable, type KeysetColumn } from '@/components/KeysetTable/KeysetTable';
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import { labels as contractLabels } from '@/contracts';
import platform from '../../../../../backend/resources/labels.platform.es.json';
import {
  detailClock,
  detailPort,
  readLabels,
  REASON_CATEGORIES,
  type Label,
  type LabelFilters,
  type Plant,
} from '@/ports/scr08Detail';
import { policyFor, screenKey, useScreenQuery } from '@/state/query';
import { useUiSession } from '@/state/session';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import { reasonLabel } from '../detail/DetailView';

const FAMILIES: readonly string[] = Object.keys(contractLabels.predicate_family);
const roles: Readonly<Record<string, string>> = platform.role;

/** Día local `AAAA-MM-DD` del campo de fecha → instante UTC de su comienzo, o `null`. */
function dayStart(value: string): string | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
  const instant = Date.parse(`${value}T00:00:00.000Z`);
  return Number.isFinite(instant) ? new Date(instant).toISOString() : null;
}

interface Applied {
  readonly run: number;
  readonly filters: LabelFilters;
}

export default function Page() {
  const { principal } = useUiSession();
  const scope = `${principal?.organization.organization_id ?? 'none'}:${
    principal?.assignments
      .map((a) => `${a.scope_level}:${a.scope_id}`)
      .sort()
      .join(',') ?? ''
  }`;
  const hierarchy = useScreenQuery({
    key: screenKey({
      screen: 'scr08',
      operation: 'GET /hierarchy',
      scope,
      concession: principal?.concession_id ?? null,
    }),
    policy: policyFor('GET /hierarchy'),
    read: ({ signal }) => detailPort.hierarchy.read({}, signal),
  });
  const plants: readonly Plant[] = hierarchy.data?.plants ?? [];
  const zones = plants.flatMap((plant) =>
    plant.zones.map((zone) => ({ ...zone, timezone: plant.timezone })),
  );
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const [zoneId, setZoneId] = useState('');
  const [family, setFamily] = useState('');
  const [reason, setReason] = useState('');
  const [invalid, setInvalid] = useState(false);
  const [applied, setApplied] = useState<Applied | null>(null);

  const submit = (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    const start = dayStart(from);
    const endDay = dayStart(to);
    const end = endDay === null ? null : new Date(Date.parse(endDay) + 86_400_000).toISOString();
    if (start === null || end === null || Date.parse(end) <= Date.parse(start)) {
      setInvalid(true);
      return;
    }
    setInvalid(false);
    setApplied((current) => ({
      run: (current?.run ?? 0) + 1,
      filters: {
        from: start,
        to: end,
        ...(zoneId === '' ? {} : { zone_id: zoneId }),
        ...(family === '' ? {} : { family }),
        ...(reason === '' ? {} : { reason_category: reason }),
      },
    }));
  };

  const columns: readonly KeysetColumn<Label>[] = [
    {
      id: 'zone',
      header: text('scr08.labels.col_zone'),
      cell: (row) =>
        zones.find((zone) => zone.zone_id === row.zone_id)?.name ?? text('common.unknown'),
    },
    {
      id: 'family',
      header: text('scr08.labels.col_family'),
      cell: (row) => label('predicate_family', row.family),
    },
    {
      id: 'outcome',
      header: text('scr08.labels.col_outcome'),
      cell: (row) => label('classification_outcome', row.outcome),
    },
    {
      id: 'reason',
      header: text('scr08.labels.col_reason'),
      cell: (row) => reasonLabel(row.outcome, row.reason_category),
    },
    {
      id: 'labeled_at',
      header: text('scr08.labels.col_labeled_at'),
      cell: (row) => (
        <TimestampWithOffset
          instant={row.labeled_at}
          timeZone={zones.find((zone) => zone.zone_id === row.zone_id)?.timezone ?? 'UTC'}
        />
      ),
    },
    {
      id: 'role',
      header: text('scr08.labels.col_role'),
      cell: (row) =>
        row.labeled_by_role !== null && Object.hasOwn(roles, row.labeled_by_role)
          ? (roles[row.labeled_by_role] ?? text('common.unknown'))
          : text('common.unknown'),
    },
  ];

  return (
    <main>
      <nav aria-label={text('scr08.detail.navigation')}>
        <Link to="/findings">{text('scr08.detail.back')}</Link>
      </nav>
      <h1>{text('scr08.labels.title')}</h1>
      <p>{text('scr08.labels.audited')}</p>
      <form aria-label={text('scr08.labels.filters')} noValidate onSubmit={submit}>
        <label>
          {text('scr08.labels.from')}
          <input
            type="date"
            name="from"
            required
            value={from}
            aria-invalid={invalid}
            onChange={(event) => {
              setFrom(event.currentTarget.value);
            }}
          />
        </label>
        <label>
          {text('scr08.labels.to')}
          <input
            type="date"
            name="to"
            required
            value={to}
            aria-invalid={invalid}
            onChange={(event) => {
              setTo(event.currentTarget.value);
            }}
          />
        </label>
        <label>
          {text('scr08.labels.zone')}
          <select
            name="zone_id"
            value={zoneId}
            onChange={(event) => {
              setZoneId(event.currentTarget.value);
            }}
          >
            <option value="">{text('scr08.labels.any')}</option>
            {zones.map((zone) => (
              <option key={zone.zone_id} value={zone.zone_id}>
                {zone.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          {text('scr08.labels.family')}
          <select
            name="family"
            value={family}
            onChange={(event) => {
              setFamily(event.currentTarget.value);
            }}
          >
            <option value="">{text('scr08.labels.any')}</option>
            {FAMILIES.map((value) => (
              <option key={value} value={value}>
                {label('predicate_family', value)}
              </option>
            ))}
          </select>
        </label>
        <label>
          {text('scr08.labels.reason_category')}
          <select
            name="reason_category"
            value={reason}
            onChange={(event) => {
              setReason(event.currentTarget.value);
            }}
          >
            <option value="">{text('scr08.labels.any')}</option>
            {REASON_CATEGORIES.map((item) => (
              <option key={item.value} value={item.value}>
                {label(item.list, item.value)}
              </option>
            ))}
          </select>
        </label>
        <button type="submit">{text('scr08.labels.submit')}</button>
        {invalid && <p role="alert">{text('scr08.labels.invalid_period')}</p>}
      </form>
      {applied !== null && (
        <KeysetTable<Label>
          key={applied.run}
          caption={text('scr08.labels.list')}
          columns={columns}
          rowKey={(row) => row.label_id}
          clock={detailClock()}
          emptyText={text('scr08.labels.empty')}
          fetchPage={(request, signal) =>
            readLabels(applied.filters, request.cursor, request.pageSize, signal)
          }
        />
      )}
    </main>
  );
}
