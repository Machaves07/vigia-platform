/** Ventana acotada y foco por índice lógico: nodos, cámaras y zonas son filas independientes. */
import { useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { text } from '@/texts';
import styles from './inventory.module.css';

export interface FleetRow {
  readonly id: string;
  readonly cells: readonly ReactNode[];
}
const ROW_HEIGHT = 208;
const VISIBLE_ROWS = 3;
const OVERSCAN = 3;
const VIRTUAL_THRESHOLD = 300;

export function VirtualFleetTable({
  rows,
  headings,
  caption,
}: {
  readonly rows: readonly FleetRow[];
  readonly headings: readonly string[];
  readonly caption: string;
}) {
  const virtual = rows.length > VIRTUAL_THRESHOLD;
  const [start, setStart] = useState(0);
  const [active, setActive] = useState(0);
  const viewport = useRef<HTMLDivElement>(null);
  const pendingFocus = useRef<number | null>(null);
  const first = virtual ? Math.max(0, Math.min(start, rows.length - VISIBLE_ROWS) - OVERSCAN) : 0;
  const last = virtual ? Math.min(rows.length, first + VISIBLE_ROWS + OVERSCAN * 2) : rows.length;
  useLayoutEffect(() => {
    if (pendingFocus.current === null) return;
    viewport.current
      ?.querySelector<HTMLElement>(`[data-index="${String(pendingFocus.current)}"]`)
      ?.focus({ preventScroll: true });
    pendingFocus.current = null;
  });
  function move(index: number) {
    const next = Math.max(0, Math.min(rows.length - 1, index));
    pendingFocus.current = next;
    setActive(next);
    if (virtual && viewport.current !== null) {
      viewport.current.scrollTop = next * ROW_HEIGHT;
      setStart(next);
    }
  }
  return (
    <>
      <p>{text('scr07.keyboard')}</p>
      <div
        ref={viewport}
        className={virtual ? styles.viewport : styles.tableWrap}
        onScroll={(event) => {
          if (virtual) setStart(Math.floor(event.currentTarget.scrollTop / ROW_HEIGHT));
        }}
      >
        <table className={styles.table} aria-rowcount={rows.length + 1}>
          <caption>{caption}</caption>
          <thead>
            <tr>
              {headings.map((heading) => (
                <th key={heading} scope="col">
                  {heading}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {first > 0 && (
              <tr aria-hidden="true">
                <td colSpan={headings.length} style={{ height: first * ROW_HEIGHT, padding: 0 }} />
              </tr>
            )}
            {rows.slice(first, last).map((row, offset) => {
              const index = first + offset;
              return (
                <tr
                  key={row.id}
                  data-index={index}
                  aria-rowindex={index + 2}
                  tabIndex={
                    index === active || ((active < first || active >= last) && index === first)
                      ? 0
                      : -1
                  }
                  onFocus={() => {
                    setActive(index);
                  }}
                  onKeyDown={(event) => {
                    const target = {
                      ArrowDown: index + 1,
                      ArrowUp: index - 1,
                      Home: 0,
                      End: rows.length - 1,
                    }[event.key];
                    if (target === undefined) return;
                    event.preventDefault();
                    move(target);
                  }}
                  className={[styles.row, virtual ? styles.virtualRow : undefined]
                    .filter(Boolean)
                    .join(' ')}
                >
                  {row.cells.map((cell, column) => (
                    <td key={column}>
                      {/* Solo la ventana virtual necesita la altura fija de la celda: fuera de
                          ella, una celda desplazable sería inalcanzable con teclado (NFR-APP-50). */}
                      {virtual ? <div className={styles.cell}>{cell}</div> : cell}
                    </td>
                  ))}
                </tr>
              );
            })}
            {last < rows.length && (
              <tr aria-hidden="true">
                <td
                  colSpan={headings.length}
                  style={{ height: (rows.length - last) * ROW_HEIGHT, padding: 0 }}
                />
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </>
  );
}
