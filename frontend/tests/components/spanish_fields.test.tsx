/**
 * VIG-366 · «Español sin avisos nativos» y «Hora de la planta»: los campos de fecha, fecha y hora,
 * y mes propios (`DateFields`), la comprobación propia de formularios (`useFormCheck`), la zona
 * una vez por tabla (`ZoneHeader` y `zone="header"`), la paginación (`Pager`), las duraciones y
 * el resumen del navegador. Se comparan valores enteros y claves del catálogo, nunca subcadenas.
 */
import { fireEvent, render, screen, within } from '@testing-library/react';
import { useState } from 'react';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';

import { DateInput, DateTimeInput, MonthInput } from '@/components/DateFields/DateFields';
import {
  dateText,
  parseDateText,
  parseTimeText,
  recentMonths,
} from '@/components/DateFields/parse';
import { useFormCheck } from '@/components/DateFields/useFormCheck';
import { Pager, usePaged } from '@/components/Pager/Pager';
import {
  TimestampWithOffset,
  ZoneHeader,
} from '@/components/TimestampWithOffset/TimestampWithOffset';
import { clientSummary } from '@/pages/scr01/account/clientHint';
import { text } from '@/texts';
import { formatDuration } from '@/texts/duration';
import { capitalized, label } from '@/texts/labels';

function DateHarness({ initial = '' }: { readonly initial?: string }) {
  const [value, setValue] = useState(initial);
  return (
    <>
      <label htmlFor="fecha">Fecha</label>
      <DateInput id="fecha" name="fecha" value={value} onChange={setValue} />
      <output data-testid="value">{value}</output>
    </>
  );
}

function DateTimeHarness() {
  const [value, setValue] = useState('');
  return (
    <>
      <label htmlFor="momento">Momento</label>
      <DateTimeInput id="momento" value={value} onChange={setValue} />
      <output data-testid="value">{value}</output>
    </>
  );
}

function FormHarness({ onValid }: { readonly onValid: () => void }) {
  const check = useFormCheck();
  return (
    <form
      aria-label="Formulario"
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        if (check.run(event.currentTarget)) onValid();
      }}
    >
      <label>
        Ventana
        <input
          name="window"
          type="number"
          min="15"
          max="480"
          step="1"
          required
          {...check.field('window')}
          onChange={() => {
            check.clear('window');
          }}
        />
        {check.error('window')}
      </label>
      <label>
        Día
        <DateInput name="day" required {...check.field('day')} />
        {check.error('day')}
      </label>
      <button type="submit">Enviar</button>
    </form>
  );
}

function PagedList({ count, more = false }: { readonly count: number; readonly more?: boolean }) {
  const items = Array.from({ length: count }, (_, index) => `Elemento ${String(index + 1)}`);
  const paged = usePaged(items);
  return (
    <>
      <ul aria-label="Lista">
        {paged.visible.map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
      <Pager
        page={paged.page}
        total={items.length}
        onPage={paged.setPage}
        more={more}
        onLoadMore={() => undefined}
        label="Páginas"
      />
    </>
  );
}

describe('campos de fecha en español (VIG-366)', () => {
  it('la fecha se escribe dd/mm/aaaa y el valor es ISO; un texto que no es fecha da su error', () => {
    render(<DateHarness />);
    const input = screen.getByRole('textbox', { name: 'Fecha' });
    expect(input.getAttribute('placeholder')).toBe('dd/mm/aaaa');
    expect(input.getAttribute('type')).toBe('text');
    fireEvent.change(input, { target: { value: '05/10/2026' } });
    expect(screen.getByTestId('value').textContent).toBe('2026-10-05');
    expect(document.querySelector<HTMLInputElement>('input[type="hidden"]')?.value).toBe(
      '2026-10-05',
    );
    fireEvent.change(input, { target: { value: '31/02/2026' } });
    fireEvent.blur(input);
    expect(screen.getByTestId('value').textContent).toBe('');
    expect(input.getAttribute('aria-invalid')).toBe('true');
    const error = document.getElementById(input.getAttribute('aria-describedby') ?? '');
    expect(error?.getAttribute('data-text-key')).toBe('components_dates.date_format');
    expect((input as HTMLInputElement).validationMessage).toBe(
      text('components_dates.date_format'),
    );
  });

  it('dentro de su etiqueta, el error no cambia el nombre accesible del campo: es su descripción', () => {
    render(
      <label>
        Desde
        <DateInput name="from" />
      </label>,
    );
    const input = screen.getByRole('textbox', { name: 'Desde' });
    fireEvent.change(input, { target: { value: '99/99/2026' } });
    fireEvent.blur(input);
    expect(screen.getByRole('textbox', { name: 'Desde' })).toBe(input);
    expect(input.getAttribute('aria-invalid')).toBe('true');
    expect(document.getElementById(input.getAttribute('aria-describedby') ?? '')?.textContent).toBe(
      text('components_dates.date_format'),
    );
  });

  it('un valor controlado se muestra como dd/mm/aaaa y vaciarlo desde fuera vacía el campo', () => {
    function Reset() {
      const [value, setValue] = useState('2026-01-09');
      return (
        <>
          <DateInput aria-label="Fecha" value={value} onChange={setValue} />
          <button
            type="button"
            onClick={() => {
              setValue('');
            }}
          >
            Vaciar
          </button>
        </>
      );
    }
    render(<Reset />);
    const input = screen.getByRole<HTMLInputElement>('textbox', { name: 'Fecha' });
    expect(input.value).toBe('09/01/2026');
    fireEvent.click(screen.getByRole('button', { name: 'Vaciar' }));
    expect(input.value).toBe('');
  });

  it('fecha y hora: el valor solo existe con las dos válidas, en 24 horas', () => {
    render(<DateTimeHarness />);
    const date = screen.getByRole('textbox', { name: 'Momento' });
    const time = screen.getByRole('textbox', { name: text('components_dates.time_label') });
    fireEvent.change(date, { target: { value: '29/10/2026' } });
    expect(screen.getByTestId('value').textContent).toBe('');
    fireEvent.change(time, { target: { value: '14:30' } });
    expect(screen.getByTestId('value').textContent).toBe('2026-10-29T14:30');
    fireEvent.change(time, { target: { value: '2:30 PM' } });
    expect(screen.getByTestId('value').textContent).toBe('');
  });

  it('el mes es una lista con nombres en español, del más reciente al más antiguo', () => {
    render(<MonthInput aria-label="Mes" latest="2026-10" count={3} defaultValue="" />);
    const select = screen.getByRole('combobox', { name: 'Mes' });
    expect(
      within(select)
        .getAllByRole('option')
        .map((option) => option.textContent),
    ).toEqual([
      text('components_dates.month_choose'),
      'Octubre de 2026',
      'Septiembre de 2026',
      'Agosto de 2026',
    ]);
  });

  it('las funciones de análisis aceptan solo fechas y horas reales', () => {
    expect(parseDateText('29/02/2024')).toBe('2024-02-29');
    expect(parseDateText('29/02/2026')).toBeNull();
    expect(parseDateText('10/29/2026')).toBeNull();
    expect(parseDateText('2026-10-29')).toBeNull();
    expect(dateText('2026-10-29')).toBe('29/10/2026');
    expect(parseTimeText('23:59')).toBe('23:59');
    expect(parseTimeText('24:00')).toBeNull();
    expect(recentMonths('2026-01', 3)).toEqual(['2026-01', '2025-12', '2025-11']);
  });

  it('pasa axe', async () => {
    const { container } = render(<DateHarness initial="2026-10-05" />);
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe('useFormCheck: errores propios en español en lugar de la burbuja nativa', () => {
  it('obligatorio y fuera de rango se dicen junto al campo con aria-invalid y aria-describedby', () => {
    let sent = 0;
    render(
      <FormHarness
        onValid={() => {
          sent += 1;
        }}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Enviar' }));
    expect(sent).toBe(0);
    const windowField = screen.getByRole('spinbutton', { name: /^Ventana/ });
    expect(windowField.getAttribute('aria-invalid')).toBe('true');
    const error = document.getElementById(windowField.getAttribute('aria-describedby') ?? '');
    expect(error?.getAttribute('data-text-key')).toBe('validation.required');
    expect(document.activeElement).toBe(windowField);
    fireEvent.change(windowField, { target: { value: '5' } });
    fireEvent.click(screen.getByRole('button', { name: 'Enviar' }));
    const range = document.getElementById(windowField.getAttribute('aria-describedby') ?? '');
    expect(range?.getAttribute('data-text-key')).toBe('components_dates.min');
    expect(range?.textContent).toBe(`${text('components_dates.min')} 15.`);
    const day = screen.getByRole('textbox', { name: /^Día/ });
    expect(day.getAttribute('aria-invalid')).toBe('true');
  });

  it('con todo válido envía una sola vez', () => {
    let sent = 0;
    render(
      <FormHarness
        onValid={() => {
          sent += 1;
        }}
      />,
    );
    fireEvent.change(screen.getByRole('spinbutton', { name: /^Ventana/ }), {
      target: { value: '30' },
    });
    fireEvent.change(screen.getByRole('textbox', { name: /^Día/ }), {
      target: { value: '01/10/2026' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Enviar' }));
    expect(sent).toBe(1);
  });
});

describe('hora de la planta: la zona una sola vez por tabla', () => {
  it('ZoneHeader nombra la zona y la celda con zone="header" no la repite', () => {
    render(
      <table>
        <thead>
          <tr>
            <th>
              Momento
              <ZoneHeader timeZone="America/Bogota" />
            </th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td>
              <TimestampWithOffset
                instant="2026-10-29T17:05:09.000Z"
                timeZone="America/Bogota"
                zone="header"
              />
            </td>
          </tr>
        </tbody>
      </table>,
    );
    expect(document.querySelectorAll('[data-part="zone_header"]')).toHaveLength(1);
    expect(document.querySelectorAll('td [data-part="zone"]')).toHaveLength(0);
    expect(document.querySelector('td time')?.textContent).toBe('29/10/2026, 12:05:09');
    expect(document.querySelector('td time')?.getAttribute('dateTime')).toBe(
      '2026-10-29T17:05:09.000Z',
    );
  });

  it('un plazo se muestra solo con la fecha, sin «00:00:00» (S3-25)', () => {
    render(
      <TimestampWithOffset instant="2026-11-03T05:00:00.000Z" timeZone="America/Bogota" dateOnly />,
    );
    expect(document.querySelector('time')?.textContent).toBe('03/11/2026');
  });
});

describe('paginación «1–20 de 37»', () => {
  it('pagina de 20 en 20 con Anterior y Siguiente', () => {
    render(<PagedList count={37} />);
    expect(screen.getAllByRole('listitem')).toHaveLength(20);
    const pager = screen.getByRole('navigation', { name: 'Páginas' });
    expect(pager.querySelector('[data-part="range"]')?.textContent).toBe('1–20 de 37');
    expect(
      within(pager).getByRole<HTMLButtonElement>('button', { name: 'Anterior' }).disabled,
    ).toBe(true);
    fireEvent.click(within(pager).getByRole('button', { name: 'Siguiente' }));
    expect(screen.getAllByRole('listitem')).toHaveLength(17);
    expect(pager.querySelector('[data-part="range"]')?.textContent).toBe('21–37 de 37');
    expect(
      within(pager).getByRole<HTMLButtonElement>('button', { name: 'Siguiente' }).disabled,
    ).toBe(true);
  });

  it('con más en el servidor dice «o más» y Siguiente sigue disponible; con una página, no aparece', () => {
    const { unmount } = render(<PagedList count={5} more />);
    const pager = screen.getByRole('navigation', { name: 'Páginas' });
    expect(pager.querySelector('[data-part="range"]')?.textContent).toBe('1–5 de 5 o más');
    expect(
      within(pager).getByRole<HTMLButtonElement>('button', { name: 'Siguiente' }).disabled,
    ).toBe(false);
    unmount();
    render(<PagedList count={5} />);
    expect(screen.queryByRole('navigation', { name: 'Páginas' })).toBeNull();
  });
});

describe('personas, no sistemas', () => {
  it('las duraciones van en segundos o minutos, nunca en milisegundos', () => {
    expect(formatDuration(4167)).toBe('4,2 s');
    expect(formatDuration(0)).toBe('0 s');
    expect(formatDuration(59_960)).toBe('1 min');
    expect(formatDuration(65_000)).toBe('1 min 5 s');
    expect(formatDuration(3_720_000)).toBe('1 h 2 min');
    expect(formatDuration(-1)).toBeNull();
    expect(formatDuration(Number.NaN)).toBeNull();
  });

  it('el navegador y el sistema se resumen; lo que no se reconoce no se adivina', () => {
    const chrome = [
      'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
      'AppleWebKit/537.36 (KHTML, like Gecko)',
      'Chrome/130.0.0.0 Safari/537.36',
    ].join(' ');
    expect(clientSummary(chrome)).toEqual({ browser: 'Chrome', system: 'Windows' });
    const edge = `${chrome} Edg/130.0.0.0`;
    expect(clientSummary(edge)).toEqual({ browser: 'Edge', system: 'Windows' });
    expect(clientSummary('Navegador sintético')).toEqual({ browser: null, system: null });
  });

  it('las etiquetas de las listas cerradas llevan mayúscula inicial', () => {
    expect(label('classification_outcome', 'confirmed')).toBe(
      capitalized(label('classification_outcome', 'confirmed')),
    );
    expect(label('classification_outcome', 'confirmed').charAt(0)).toBe('C');
    expect(label('role', 'coordinator_sst').charAt(0)).toBe('C');
  });
});
