/**
 * UI-16 `SignatureBlock` (BR-APP-47, 43): nombre y licencia como instantánea, advertencia sin
 * licencia antes de firmar, guía ante `loop_signer_license_missing` y ningún campo de licencia
 * editable en el árbol accesible. Se comparan textos exactos del catálogo.
 */
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { axe } from 'vitest-axe';

import { FakeClock } from '../support/fakeClock';
import { CopyOnceSecret } from '@/components/CopyOnceSecret/CopyOnceSecret';
import { NoticeStack } from '@/components/NoticeStack/NoticeStack';
import type { Notice } from '@/components/NoticeStack/notice';
import { ReasonField } from '@/components/ReasonField/ReasonField';
import {
  SIGNER_LICENSE_MISSING,
  SignatureBlock,
  type SignerProfile,
} from '@/components/SignatureBlock/SignatureBlock';
import { WriteReceipt } from '@/components/WriteReceipt/WriteReceipt';
import { text } from '@/texts';

const LICENSED: SignerProfile = { display_name: 'Ana Ruiz', professional_license: 'LIC-SST-0042' };
const UNLICENSED: SignerProfile = { display_name: 'Luis Gómez', professional_license: null };
const TEXT = 'Acepto el análisis de causa raíz tal como aparece arriba.';

const licenseMissing: Notice = {
  id: 'sig',
  error: { code: 'invalid_request', status: 422, detail_code: SIGNER_LICENSE_MISSING },
  correlation_id: '0190a3b2-7c4d-7e8f-9a0b-1c2d3e4f5a6b',
};

function block(): HTMLElement {
  return screen.getByRole('region', { name: text('components_forms.signature.title') });
}

function part(name: string): string | null {
  return block().querySelector(`[data-part="${name}"]`)?.textContent ?? null;
}

function signButton(): HTMLButtonElement {
  return screen.getByRole('button', { name: text('components_forms.signature.sign') });
}

describe('UI-16 con licencia', () => {
  it('muestra nombre y licencia del perfil, sin advertencia ni guía', () => {
    render(<SignatureBlock principal={LICENSED} textToSign={TEXT} onSign={() => undefined} />);
    expect(part('name')).toBe('Ana Ruiz');
    expect(part('license')).toBe('LIC-SST-0042');
    expect(part('text_to_sign')).toBe(TEXT);
    expect(part('warning')).toBeNull();
    expect(part('guide')).toBeNull();
  });

  it('es una instantánea: un cambio del perfil tras montar no altera lo que se firma', () => {
    const onSign = vi.fn();
    const { rerender } = render(
      <SignatureBlock principal={LICENSED} textToSign={TEXT} onSign={onSign} />,
    );
    rerender(
      <SignatureBlock
        principal={{ display_name: 'Otro nombre', professional_license: 'LIC-9' }}
        textToSign={TEXT}
        onSign={onSign}
      />,
    );
    expect(part('name')).toBe('Ana Ruiz');
    fireEvent.click(signButton());
    expect(onSign).toHaveBeenCalledExactlyOnceWith(LICENSED);
  });
});

describe('UI-16 sin licencia', () => {
  it.each([
    ['nula', null],
    ['vacía', ''],
    ['solo espacios', '   '],
  ])(
    'licencia %s: advierte antes de firmar sin deshabilitar la firma (BR-APP-43)',
    (_name, license) => {
      const onSign = vi.fn();
      render(
        <SignatureBlock
          principal={{ display_name: 'Luis Gómez', professional_license: license }}
          textToSign={TEXT}
          onSign={onSign}
        />,
      );
      expect(onSign).not.toHaveBeenCalled();
      expect(screen.getByRole('note').textContent).toBe(
        text('components_forms.signature.warning_no_license'),
      );
      expect(part('license')).toBe(text('components_forms.signature.license_absent'));
      expect(part('guide')).toBeNull();
      expect(signButton().disabled).toBe(false);
      fireEvent.click(signButton());
      expect(onSign).toHaveBeenCalledOnce();
    },
  );

  it('ante loop_signer_license_missing muestra la guía y la etiqueta de U-04', () => {
    render(
      <SignatureBlock
        principal={UNLICENSED}
        textToSign={TEXT}
        onSign={() => undefined}
        notice={licenseMissing}
      />,
    );
    expect(part('guide')).toBe(text('components_forms.signature.guide_license_missing'));
    expect(screen.getByRole('alert').getAttribute('data-code')).toBe('invalid_request');
  });

  it('otro error de firma no muestra la guía', () => {
    render(
      <SignatureBlock
        principal={UNLICENSED}
        textToSign={TEXT}
        onSign={() => undefined}
        notice={{
          ...licenseMissing,
          error: { code: 'conflict', status: 409, detail_code: 'loop_signature_window_closed' },
        }}
      />,
    );
    expect(part('guide')).toBeNull();
  });

  it('la guía aparece aunque GET /me traiga licencia: manda el servidor', () => {
    render(
      <SignatureBlock
        principal={LICENSED}
        textToSign={TEXT}
        onSign={() => undefined}
        notice={licenseMissing}
      />,
    );
    expect(part('guide')).toBe(text('components_forms.signature.guide_license_missing'));
  });
});

describe('BR-APP-47: la licencia nunca se captura como texto libre', () => {
  it.each([
    ['con licencia', LICENSED, undefined],
    ['sin licencia', UNLICENSED, undefined],
    ['con la guía', UNLICENSED, licenseMissing],
  ])('%s: ningún control editable en el árbol accesible', (_name, principal, notice) => {
    const { container } = render(
      <SignatureBlock
        principal={principal}
        textToSign={TEXT}
        onSign={() => undefined}
        notice={notice}
      />,
    );
    for (const role of ['textbox', 'searchbox', 'combobox', 'spinbutton', 'listbox'] as const) {
      expect(screen.queryAllByRole(role)).toEqual([]);
    }
    expect(container.querySelectorAll('input, textarea, select, [contenteditable]')).toHaveLength(
      0,
    );
    expect(screen.getAllByRole('button').map((button) => button.textContent)).toEqual(
      notice === undefined
        ? [text('components_forms.signature.sign')]
        : [text('components_forms.notice.copy'), text('components_forms.signature.sign')],
    );
  });
});

describe('axe en los cinco componentes', () => {
  it.each([
    [
      'ReasonField',
      <ReasonField
        key="r"
        label="Motivo"
        value=""
        onChange={() => undefined}
        limits={{ minLength: 10, maxLength: 500 }}
        showErrors
      />,
    ],
    [
      'NoticeStack',
      <NoticeStack
        key="n"
        clock={new FakeClock()}
        onDismiss={() => undefined}
        onRetry={() => undefined}
        notices={[{ id: 'x', error: { code: 'throttled', status: 429, retry_after_seconds: 2 } }]}
      />,
    ],
    [
      'WriteReceipt',
      <WriteReceipt key="w" record="Firma del análisis" roleInUse="coordinator_sst" />,
    ],
    [
      'CopyOnceSecret',
      <CopyOnceSecret
        key="c"
        kind="invitation_link"
        value="https://x.example/i/1"
        onAcknowledged={() => undefined}
      />,
    ],
    [
      'SignatureBlock sin licencia y con la guía',
      <SignatureBlock
        key="s"
        principal={UNLICENSED}
        textToSign={TEXT}
        onSign={() => undefined}
        notice={licenseMissing}
      />,
    ],
    [
      'SignatureBlock con licencia',
      <SignatureBlock key="l" principal={LICENSED} textToSign={TEXT} onSign={() => undefined} />,
    ],
  ])('%s', async (_name, ui) => {
    const { container } = render(<main>{ui}</main>);
    const results = await axe(container);
    const serious = results.violations.filter(
      (violation) => violation.impact === 'serious' || violation.impact === 'critical',
    );
    expect(serious).toEqual([]);
    expect(results).toHaveNoViolations();
  });
});
