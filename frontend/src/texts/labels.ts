/** Etiquetas de U-01 a U-04 incorporadas en la construcción, nunca códigos como fallback. */
import { labels } from '@/contracts';
import platform from '../../../backend/resources/labels.platform.es.json';
import loop from '../../../backend/resources/labels.loop.es.json';
import app from './app_labels.json';
import { text } from './index';

const tables = new Map<string, Map<string, string>>();
const sources: readonly Readonly<Record<string, Readonly<Record<string, string>>>>[] = [
  labels,
  platform,
  loop,
];
for (const source of sources) {
  for (const [name, values] of Object.entries(source)) {
    const table = tables.get(name) ?? new Map<string, string>();
    for (const [value, caption] of Object.entries(values)) {
      if (typeof caption !== 'string') throw new Error(`Etiqueta inválida: ${name}.${value}`);
      table.set(value, caption);
    }
    tables.set(name, table);
  }
}
for (const [key, caption] of Object.entries(app)) {
  const [, name, value] = key.split('.');
  if (name === undefined || value === undefined) throw new Error(`Etiqueta inválida: ${key}`);
  const table = tables.get(name) ?? new Map<string, string>();
  table.set(value, caption);
  tables.set(name, table);
}

/** `enumName` es el nombre canónico snake_case de la lista de la unidad dueña. */
export function label(enumName: string, value: string): string {
  return tables.get(enumName)?.get(value) ?? text('common.unknown');
}
