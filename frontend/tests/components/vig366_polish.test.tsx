/**
 * VIG-366 · «Sin datos técnicos crudos» y «Pulido»: el vocabulario muestra el patrón por su nombre
 * con la expresión plegada, el acceso lleva la marca, el detalle nombra el tramo sin «desconocido»
 * y el nivel de evidencia en español, y la miga hacia las etiquetas se entiende. Se comparan
 * textos enteros del catálogo y atributos, nunca subcadenas. Datos sintéticos.
 */
import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { AuthFrame } from '@/pages/scr01/login/AuthFrame';
import { providerColumns, providerScopeText } from '@/pages/scr02/provider/ProviderView';
import { DetailView } from '@/pages/scr08/detail/DetailView';
import { VocabularyView } from '@/pages/scr04/vocabulary/VocabularyView';
import type { FindingDetail } from '@/ports/scr08Detail';
import type { Vocabulary } from '@/ports/scr04Parameters';
import { text } from '@/texts';
import { label } from '@/texts/labels';

import { providerConcession } from './scr02/support';
import { detail } from './scr08/detail_support';

const PATTERN = String.raw`\bsin\s+riesgo\b`;

function vocabulary(): Vocabulary {
  const base = {
    category: 'absence_claim',
    suggestion_es: 'Describa lo observado',
    origin: 'derived',
    added_in_version: 1,
  } as const;
  return {
    list_version: 1,
    lemmatizer_version: 'sintetico',
    effective_from: '2026-03-02T12:00:00.000Z',
    record_id: '0190a1b2-3333-7000-8000-000000000101',
    declared_names: [],
    terms: [
      {
        ...base,
        term_id: '0190a1b2-3333-7000-8000-000000000201',
        form: 'lemma',
        value: 'despejado',
        lemma: null,
      },
      {
        ...base,
        term_id: '0190a1b2-3333-7000-8000-000000000202',
        form: 'pattern',
        value: PATTERN,
        lemma: null,
      },
    ],
  };
}

function renderDetail(value: FindingDetail) {
  return render(
    <DetailView
      detail={value}
      zone={null}
      timeZone="America/Bogota"
      now={{ kind: 'hidden' }}
      records={{ kind: 'ready', records: [] }}
      clock={null}
      canViewClips={false}
    />,
  );
}

describe('VIG-366: sin datos técnicos crudos', () => {
  it('el vocabulario nombra el patrón y pliega la expresión regular en «Datos técnicos»', () => {
    const { container } = render(<VocabularyView data={vocabulary()} />);
    const cells = [...container.querySelectorAll('tbody tr')].map(
      (row) => row.querySelector('td')?.firstChild?.textContent,
    );
    expect(cells).toEqual(['despejado', text('scr04.vocabulary.pattern_readable')]);
    const folded = container.querySelectorAll('details[data-part="technical"]');
    expect(folded).toHaveLength(1);
    expect(folded[0]?.hasAttribute('open')).toBe(false);
    expect(folded[0]?.querySelector('summary')?.textContent).toBe(
      text('scr04.vocabulary.pattern_show'),
    );
    expect(folded[0]?.querySelector('dd code')?.textContent).toBe(PATTERN);
  });

  it('el tramo lleva su número y su tipo; sin número ni tipo conocido no dice «desconocido»', () => {
    const value = detail();
    const [record] = value.records;
    if (record === undefined) throw new Error('Falta el registro sintético');
    const unknown = { ...record, record_id: `${record.record_id}-2`, segment_index: null };
    const { container } = renderDetail({
      ...value,
      records: [record, { ...unknown, record_type: 'future_kind' }],
    });
    const titles = [...container.querySelectorAll('[data-part="record"] > h3')].map(
      (title) => title.textContent,
    );
    expect(titles).toEqual([
      `${text('scr08.detail.record')} 1 · ${label('record_kind', record.record_type)}`,
      text('scr08.detail.record'),
    ]);
  });

  it('las concesiones del proveedor pliegan los identificadores del cliente y del alcance', () => {
    const row = providerConcession();
    const columns = providerColumns('America/Bogota').filter((column) =>
      ['client', 'scope'].includes(column.id),
    );
    const { container } = render(
      <dl>
        {columns.map((column) => (
          <div key={column.id} data-column={column.id}>
            <dt>{column.header}</dt>
            <dd>{column.cell(row)}</dd>
          </div>
        ))}
      </dl>,
    );
    for (const [id, value] of [
      ['client', row.client_organization_id],
      ['scope', row.scope_id],
    ] as const) {
      const cell = container.querySelector(`[data-column="${id}"] > dd`);
      const folded = cell?.querySelector('details[data-part="technical"]');
      expect(folded?.hasAttribute('open')).toBe(false);
      expect(folded?.querySelector('dd code')?.textContent).toBe(value);
      // A la vista solo queda el texto del catálogo, sin el identificador.
      expect(cell?.firstChild?.textContent).toBe(
        id === 'client' ? text('scr02.provider.client_organization') : providerScopeText(row),
      );
    }
  });

  it('el nivel de evidencia se dice en español, con mayúscula, nunca «tier 2»', () => {
    const value = { ...detail(), tier: 'tier_2' as const };
    renderDetail(value);
    expect(screen.getByText(label('evidence_tier', 'tier_2'), { exact: true })).toBeDefined();
    expect(label('evidence_tier', 'tier_2')).toBe('Evidencia de nivel dos (solo visión)');
  });
});

describe('VIG-366: pulido', () => {
  it('el acceso lleva la marca y su frase, con el formulario en una tarjeta', () => {
    render(
      <AuthFrame labelledBy="titulo">
        <h1 id="titulo">Iniciar sesión</h1>
      </AuthFrame>,
    );
    const main = screen.getByRole('main', { name: 'Iniciar sesión' });
    const brand = main.querySelector('[data-part="brand"]');
    expect([...(brand?.children ?? [])].map((part) => part.textContent)).toEqual([
      text('common.app_name'),
      text('scr01.login.tagline'),
    ]);
    expect(within(main).getByRole('heading', { level: 1 }).textContent).toBe('Iniciar sesión');
  });

  it('la miga hacia las etiquetas dice adónde lleva (S3-15)', () => {
    expect(text('scr08.detail.labels_link')).toBe('Todas las etiquetas de clasificación');
  });
});
