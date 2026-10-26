/** Campos locales: avisos anclados sin descartar lo escrito ni reintentar mutaciones. */
import { useId } from 'react';
import { FieldNotice, NoticeStack } from '@/components/NoticeStack/NoticeStack';
import { isAnchored } from '@/components/NoticeStack/notice';
import { hierarchyClock, hierarchyNotice, type ValidationIssue } from '@/ports/scr01Hierarchy';
import { text } from '@/texts';
import styles from '../scr01.module.css';

export function AdminField({
  name,
  value,
  onChange,
  issues,
  type = 'text',
  min,
  max,
  disabled = false,
}: {
  readonly name: string;
  readonly value: string;
  readonly onChange: (value: string) => void;
  readonly issues: readonly ValidationIssue[];
  readonly type?: 'number' | 'text';
  readonly min?: number;
  readonly max?: number;
  readonly disabled?: boolean;
}) {
  const id = useId();
  const issue = issues.find((entry) => entry.field === name);
  return (
    <div className={styles.field}>
      <label htmlFor={id}>{text(`scr01_hierarchy.${name}`)}</label>
      <input
        className={styles.input}
        id={id}
        name={name}
        value={value}
        type={type}
        min={min}
        max={max}
        step={type === 'number' ? 1 : undefined}
        required
        disabled={disabled}
        aria-invalid={issue !== undefined}
        aria-describedby={issue === undefined ? undefined : `${id}-error`}
        onChange={(event) => {
          onChange(event.currentTarget.value);
        }}
      />
      {issue !== undefined && (
        <p id={`${id}-error`} data-text-key={issue.key}>
          {text(issue.key)}
        </p>
      )}
    </div>
  );
}

export function AdminMutationResult({
  id,
  error,
  status,
  onDismiss,
}: {
  readonly id: string;
  readonly error: Error | null;
  readonly status: string;
  readonly onDismiss: () => void;
}) {
  const notice = hierarchyNotice(error);
  return (
    <div id={id}>
      {status === 'success' && <p role="status">{text('scr01_hierarchy.saved')}</p>}
      {notice !== null &&
        (isAnchored(notice) ? (
          <FieldNotice id={`${id}-notice`} notice={notice} />
        ) : (
          <NoticeStack notices={[notice]} clock={hierarchyClock()} onDismiss={onDismiss} />
        ))}
      {error !== null && notice === null && <p role="alert">{text('screen_state.error')}</p>}
    </div>
  );
}
