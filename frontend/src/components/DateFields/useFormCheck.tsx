/**
 * Comprobación propia de un formulario con `noValidate` (VIG-366): usa las restricciones que ya
 * declaran sus campos (`required`, `min`, `max`, `step`, `pattern`, `minLength`, `maxLength`, tipo)
 * pero el mensaje es del catálogo en español, junto al campo, con `aria-invalid` y
 * `aria-describedby`; nunca la burbuja del navegador, que sale en su idioma.
 *
 * Uso: `onSubmit={(event) => { event.preventDefault(); if (!check.run(event.currentTarget)) return; … }}`
 * y, en cada campo con nombre, `{...check.field('nombre')}` y `{check.error('nombre')}`. Los campos
 * con error propio (`data-own-error`, p. ej. las fechas) lo pintan ellos; aquí solo bloquean el
 * envío.
 */
import { useId, useState, type ReactNode } from 'react';

import { FieldError } from './FieldError';

type Control = HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement;

interface Issue {
  readonly key: string;
  readonly detail?: string;
}

function isControl(element: Element): element is Control {
  return (
    element instanceof HTMLInputElement ||
    element instanceof HTMLSelectElement ||
    element instanceof HTMLTextAreaElement
  );
}

function issueOf(control: Control): Issue | null {
  const { validity } = control;
  if (validity.valid) return null;
  if (validity.valueMissing) return { key: 'validation.required' };
  if (validity.customError && control.dataset.ownError !== undefined) return { key: '' };
  if (validity.typeMismatch) {
    return { key: control.type === 'email' ? 'validation.format_email' : 'validation.type' };
  }
  if (validity.badInput) return { key: 'validation.type' };
  if (validity.rangeUnderflow)
    return { key: 'components_dates.min', detail: control.getAttribute('min') ?? '' };
  if (validity.rangeOverflow)
    return { key: 'components_dates.max', detail: control.getAttribute('max') ?? '' };
  if (validity.stepMismatch) return { key: 'components_dates.step' };
  if (validity.tooShort) return { key: 'validation.too_short' };
  if (validity.tooLong) return { key: 'validation.too_long' };
  if (validity.patternMismatch) return { key: 'validation.pattern' };
  return { key: 'validation.invalid' };
}

export interface FormCheck {
  /** Comprueba el formulario; `false` (y el foco en el primer campo inválido) si algo falla. */
  readonly run: (form: HTMLFormElement) => boolean;
  /** Atributos de accesibilidad del campo `name`. */
  readonly field: (name: string) => {
    readonly 'aria-invalid': boolean;
    readonly 'aria-describedby'?: string;
  };
  /** El error del campo `name`, o nada. */
  readonly error: (name: string) => ReactNode;
  /** Quita el error del campo `name` (al editarlo). */
  readonly clear: (name: string) => void;
}

export function useFormCheck(): FormCheck {
  const id = useId();
  const [issues, setIssues] = useState<Readonly<Record<string, Issue>>>({});
  const errorId = (name: string) => `${id}-${name}-error`;
  const shown = (name: string) => {
    const issue = issues[name];
    return issue !== undefined && issue.key !== '' ? issue : null;
  };
  return {
    run: (form) => {
      const found: Record<string, Issue> = {};
      let first: Control | null = null;
      for (const element of Array.from(form.elements)) {
        if (!isControl(element) || !element.willValidate) continue;
        const name = element.name !== '' ? element.name : element.dataset.fieldName;
        // `checkValidity` lanza `invalid`, con el que los campos con error propio lo muestran.
        if (element.checkValidity()) continue;
        const issue = issueOf(element);
        if (issue === null) continue;
        first ??= element;
        if (name !== undefined && found[name] === undefined) found[name] = issue;
      }
      setIssues(found);
      first?.focus();
      return first === null;
    },
    field: (name) => {
      const issue = shown(name);
      return issue === null
        ? { 'aria-invalid': false }
        : { 'aria-invalid': true, 'aria-describedby': errorId(name) };
    },
    error: (name) => {
      const issue = shown(name);
      if (issue === null) return null;
      return (
        <FieldError
          id={errorId(name)}
          textKey={issue.key}
          {...(issue.detail === undefined ? {} : { detail: issue.detail })}
        />
      );
    },
    clear: (name) => {
      if (issues[name] === undefined) return;
      setIssues((current) =>
        Object.fromEntries(Object.entries(current).filter(([key]) => key !== name)),
      );
    },
  };
}
