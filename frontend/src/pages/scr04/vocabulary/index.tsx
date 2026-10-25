/** SUP-U05-36/37: lista organizativa y cambios anexos; no existe edición del expediente. */
import { parametersPort } from '@/ports/scr04Parameters';
import { policyFor, screenKey, useScreenQuery } from '@/state/query';
import { text } from '@/texts';
import { Refresh, Result, useParametersScope } from '../parameters/shared';
import { VocabularyView } from './VocabularyView';
import { VocabularyForm } from './VocabularyForm';
import styles from '../parameters/parameters.module.css';

export default function Page() {
  const context = useParametersScope();
  const operation = 'GET /loop/vocabulary';
  const query = useScreenQuery({
    key: screenKey({ screen: 'scr04', operation, ...context }),
    policy: policyFor(operation),
    read: ({ signal }) => parametersPort.vocabulary.read({}, signal),
  });
  return (
    <main className={styles.page}>
      <h1>{text('scr04.vocabulary.title')}</h1>
      <p>{text('scr04.vocabulary.help')}</p>
      <Refresh query={query} />
      <Result query={query}>
        {query.data !== undefined && <VocabularyView data={query.data} />}
      </Result>
      <VocabularyForm
        key={`${context.scope}:${context.concession ?? ''}`}
        terms={query.data?.terms ?? []}
      />
    </main>
  );
}
