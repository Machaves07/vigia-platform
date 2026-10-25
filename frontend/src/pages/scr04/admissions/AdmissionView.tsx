/** Columnas y proyección RV-07: historial de solo lectura con autor y fecha del servidor. */
import type { KeysetColumn } from '@/components/KeysetTable/KeysetTable';
import { screenLabel, type Admission } from '@/ports/scr04Parameters';
import { text } from '@/texts';

export function admissionColumns(): readonly KeysetColumn<Admission>[] {
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
    { id: 'author', header: text('scr04.admissions.author'), cell: (row) => row.evaluated_by },
    { id: 'date', header: text('scr04.admissions.date'), cell: (row) => row.evaluated_at },
    { id: 'record', header: text('scr04.admissions.record'), cell: (row) => row.ledger_record_id },
  ];
}

export function AdmissionView({ data }: { readonly data: Admission }) {
  return (
    <dl>
      {admissionColumns().map((column) => (
        <div key={column.id}>
          <dt>{column.header}</dt>
          <dd>{column.cell(data)}</dd>
        </div>
      ))}
    </dl>
  );
}
