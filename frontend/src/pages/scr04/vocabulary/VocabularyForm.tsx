/** Cada añadir o retirar es un POST nuevo con motivo; el historial no ofrece acciones. */
import { useState } from 'react';
import { ReasonField } from '@/components/ReasonField/ReasonField';
import { WriteReceipt } from '@/components/WriteReceipt/WriteReceipt';
import {
  bodyIssues,
  parametersPort,
  reasonValid,
  screenLabel,
  vocabularyReasonLimits,
  type VocabularyChange,
  type Vocabulary,
} from '@/ports/scr04Parameters';
import { useScreenMutation } from '@/state/query';
import { text } from '@/texts';
import { Failure } from '../parameters/shared';
import styles from '../parameters/parameters.module.css';

const CATEGORIES = [
  'absence_claim',
  'percentage_guarantee',
  'reduction_promise',
  'intent_or_blame_attribution',
] as const;

export function VocabularyForm({ terms }: { readonly terms: Vocabulary['terms'] }) {
  const [action, setAction] = useState('add');
  const [category, setCategory] = useState<string>(CATEGORIES[0]);
  const [form, setForm] = useState('lemma');
  const [value, setValue] = useState('');
  const [suggestion, setSuggestion] = useState('');
  const [reason, setReason] = useState('');
  const [attempted, setAttempted] = useState(false);
  const [invalid, setInvalid] = useState(false);
  const [records, setRecords] = useState<{ record: string; version: number }[]>([]);
  const mutation = useScreenMutation({
    write: (body: VocabularyChange) => parametersPort.changeVocabulary.write({ body }),
    invalidates: { screen: 'scr04', resources: ['vocabulary'] },
  });
  const submit = async () => {
    if (mutation.status === 'pending') return;
    setAttempted(true);
    setInvalid(false);
    mutation.reset();
    if (!reasonValid(reason, true)) return;
    const body: VocabularyChange = {
      added: action === 'add' ? [{ category, form, value, suggestion_es: suggestion }] : [],
      removed: action === 'remove' ? [value] : [],
      declared_names_added: [],
      declared_names_removed: [],
      reason: reason.normalize('NFC'),
    };
    if (
      value.trim() === '' ||
      (action === 'add' && suggestion.trim() === '') ||
      bodyIssues('change_vocabulary_loop_vocabulary_changes_post', body).length > 0
    ) {
      setInvalid(true);
      return;
    }
    try {
      const result = await mutation.run(body);
      setRecords((current) => [
        ...current,
        { record: result.record_id, version: result.list_version },
      ]);
      setValue('');
      setSuggestion('');
      setReason('');
      setAttempted(false);
    } catch {
      /* Se conserva el borrador con el mensaje del servidor. */
    }
  };
  return (
    <>
      <form
        className={styles.form}
        aria-label={text('scr04.vocabulary.save')}
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <fieldset disabled={mutation.status === 'pending'}>
          <legend>{text('scr04.vocabulary.save')}</legend>
          <label>
            {text('scr04.vocabulary.action')}
            <select
              value={action}
              onChange={(event) => {
                setAction(event.target.value);
                setValue('');
                mutation.reset();
              }}
            >
              <option value="add">{text('scr04.vocabulary.add')}</option>
              <option value="remove">{text('scr04.vocabulary.remove')}</option>
            </select>
          </label>
          <label>
            {text(action === 'add' ? 'scr04.vocabulary.value' : 'scr04.vocabulary.term_id')}
            {action === 'add' ? (
              <input
                value={value}
                onChange={(event) => {
                  setValue(event.target.value);
                }}
              />
            ) : (
              <select
                value={value}
                onChange={(event) => {
                  setValue(event.target.value);
                }}
              >
                <option value="">{text('scr04.vocabulary.choose_term')}</option>
                {terms.map((term) => (
                  <option key={term.term_id} value={term.term_id}>
                    {term.value}
                  </option>
                ))}
              </select>
            )}
          </label>
          {action === 'add' && (
            <>
              <label>
                {text('scr04.vocabulary.category')}
                <select
                  value={category}
                  onChange={(event) => {
                    setCategory(event.target.value);
                  }}
                >
                  {CATEGORIES.map((item) => (
                    <option value={item} key={item}>
                      {screenLabel('blocked_term_category', item)}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                {text('scr04.vocabulary.form')}
                <select
                  value={form}
                  onChange={(event) => {
                    setForm(event.target.value);
                  }}
                >
                  {['lemma', 'pattern'].map((item) => (
                    <option value={item} key={item}>
                      {screenLabel('blocked_term_form', item)}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                {text('scr04.vocabulary.suggestion')}
                <input
                  value={suggestion}
                  onChange={(event) => {
                    setSuggestion(event.target.value);
                  }}
                />
              </label>
            </>
          )}
          <ReasonField
            label={text('scr04.vocabulary.reason')}
            value={reason}
            onChange={setReason}
            limits={vocabularyReasonLimits}
            showErrors={attempted}
          />
          <Failure error={mutation.error} />
          {mutation.error === null && invalid && <p role="alert">{text('validation.invalid')}</p>}
          <button type="submit">{text('scr04.vocabulary.save')}</button>
        </fieldset>
      </form>
      <section aria-label={text('scr04.vocabulary.history')}>
        <h2>{text('scr04.vocabulary.history')}</h2>
        {records.map((record) => (
          <div key={record.record}>
            <p>
              {text('scr04.vocabulary.version')}: {record.version}
            </p>
            <WriteReceipt record={record.record} roleInUse={null} />
          </div>
        ))}
      </section>
    </>
  );
}
