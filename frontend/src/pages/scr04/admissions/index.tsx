/** Tres preguntas; una negativa también se envía y queda registrada (BR-APP-46, A-56). */
import { useState } from 'react';
import { useParams } from 'react-router';
import { KeysetTable } from '@/components/KeysetTable/KeysetTable';
import { parametersClock, readAdmissions, type Admission } from '@/ports/scr04Parameters';
import { text } from '@/texts';
import { AdmissionWizard } from './AdmissionWizard';
import { admissionColumns } from './AdmissionView';
import { useParametersScope } from '../parameters/shared';
import styles from '../parameters/parameters.module.css';

export default function Page() {
  const { plant_id = '' } = useParams();
  const context = useParametersScope();
  return (
    <AdmissionPage
      key={`${context.scope}:${context.concession ?? ''}:${plant_id}`}
      plantId={plant_id}
    />
  );
}

function AdmissionPage({ plantId }: { readonly plantId: string }) {
  const [revision, setRevision] = useState(0);
  return (
    <main className={styles.page}>
      <h1>{text('scr04.admissions.title')}</h1>
      <AdmissionWizard
        plantId={plantId}
        onRecorded={() => {
          setRevision((value) => value + 1);
        }}
      />
      <KeysetTable<Admission>
        key={revision}
        caption={text('scr04.admissions.history')}
        clock={parametersClock()}
        columns={admissionColumns()}
        rowKey={(row) => row.admission_id}
        fetchPage={(request, signal) =>
          readAdmissions(plantId, request.cursor, request.pageSize, signal)
        }
      />
    </main>
  );
}
