/** Columnas y proyección RV-07: historial de solo lectura con autor y fecha del servidor. */
import type { KeysetColumn } from '@/components/KeysetTable/KeysetTable';
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import { zoneName } from '@/components/TimestampWithOffset/format';
import { TechnicalDetails } from '@/pages/shared/TechnicalDetails';
import { screenLabel, type Admission } from '@/ports/scr04Parameters';
import { text } from '@/texts';

// La respuesta no publica la zona de la planta; se explicita la del navegador, como en SCR-01, una
// sola vez en la cabecera. El autor y el registro son identificadores: van plegados (VIG-366).
export function admissionColumns(
  timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone,
): readonly KeysetColumn<Admission>[] {
  return [
    {
      id: 'family',
      header: text('scr04.admissions.family'),
      cell: (row) => screenLabel('predicate_family', row.family),
    },
    {
      id: 'result',
      header: text('scr04.admissions.result'),
      cell: (row) => screenLabel('admission_result', row.result),
    },
    {
      id: 'failed_criterion',
      header: text('scr04.admissions.failed_criterion'),
      cell: (row) =>
        row.failed_criterion === null
          ? '—'
          : screenLabel('admission_criterion', row.failed_criterion),
    },
    {
      id: 'date',
      header: zoneLabel(text('scr04.admissions.date'), timeZone),
      cell: (row) => (
        <TimestampWithOffset instant={row.evaluated_at} timeZone={timeZone} zone="header" />
      ),
    },
    {
      id: 'technical',
      header: text('common.technical_details'),
      cell: (row) => (
        <TechnicalDetails
          summary={text('scr04.admissions.technical_summary')}
          items={[
            { name: text('scr04.admissions.author'), value: row.evaluated_by },
            { name: text('scr04.admissions.record'), value: row.ledger_record_id },
          ]}
        />
      ),
    },
  ];
}

function zoneLabel(header: string, timeZone: string): string {
  const name = zoneName(timeZone);
  return name === null ? header : `${header} (${name})`;
}

export function AdmissionView({
  data,
  timeZone,
}: {
  readonly data: Admission;
  readonly timeZone?: string;
}) {
  return (
    <dl>
      {admissionColumns(timeZone).map((column) => (
        <div key={column.id}>
          <dt>{column.header}</dt>
          <dd>{column.cell(data)}</dd>
        </div>
      ))}
    </dl>
  );
}
