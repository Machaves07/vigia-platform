/** Lista propia de U-05, no copia de la lista inicial de U-04 (se prueba en Python). */
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';

/** Plegado para la lista propia; U-04 aplica además su UTS #39 real en la prueba del backend.
 * @param {string} text
 */
const fold = (text) =>
  text
    .normalize('NFKD')
    .replace(/[\p{M}\p{Cf}]/gu, '')
    .toLowerCase()
    .replace(/\s+/gu, ' ');
const WORD = '[\\p{L}\\p{N}]';
/** @param {string} pattern */
const bounded = (pattern) => new RegExp(`(?<!${WORD})(?:${pattern})(?!${WORD})`, 'u');
// D-9: se tapa solo la expresión permitida, nunca el texto entero que la contiene.
const NEGATED =
  /(?<![\p{L}\p{N}])no[\W_]+(?:significa|equivale[\W_]+a)[\W_]+zona[\W_]+(?:despejada|segura)(?![\p{L}\p{N}])/gu;
const BLOCKED = [
  'segur[oa]s?',
  'despejad[oa]s?',
  'vaci[oa]s?',
  'sabotajes?',
  'manipulacion[\\W_]+deliberada',
  'garantiz[a-z]*',
  'garantias?',
  'reduce[\\W_]+accidentes',
  'eficiencia',
  'productividad',
  'desempeno',
  '0[\\W_]+hallazgos',
  'no[\\W_]+ocurrio',
  '[0-9]+(?:[.,][0-9]+)?[\\W_]*%[\\W_]*(?:de[\\W_]+)?deteccion',
  'deteccion[\\W_]+(?:del?[\\W_]+)?[0-9]+(?:[.,][0-9]+)?[\\W_]*%',
  'promet[a-z]*',
  'compromiso[\\W_]+de[\\W_]+(?:calidad|adopcion|activacion)',
  'asegura[a-z]*',
].map(bounded);
const INTENT = bounded(
  'intencional(?:es)?|deliberad[oa]s?|adrede|a[\\W_]+proposito|culpable|negligencia',
);

/** @param {string} key @param {string} text @returns {string[]} */
export function textViolations(key, text) {
  const form = fold(text);
  const masked = form.replace(NEGATED, ' ');
  const errors = BLOCKED.flatMap((pattern) => {
    const found = pattern.exec(masked);
    return found ? [`${key}: término bloqueado «${found[0]}»`] : [];
  });
  if (/(?:^|\.)goal/.test(key) && !bounded('objetivo propio').test(form)) {
    errors.push(`${key}: falta la marca «objetivo propio»`);
  }
  if (/(?:^|\.)goal/.test(key) && bounded('compromiso').test(form)) {
    errors.push(`${key}: una meta no se presenta como compromiso`);
  }
  if (bounded('certeza').test(form) && /confidence|confianza/.test(`${key} ${form}`)) {
    errors.push(`${key}: confianza no se presenta como certeza`);
  }
  if (/degrad|alarm|coverage|cobertura/.test(`${key} ${form}`) && INTENT.test(masked)) {
    errors.push(`${key}: etiqueta de degradación con intención`);
  }
  return errors;
}

/** Escanea todos los dominios sin manifiesto. Estructura idéntica al agregador de construcción.
 * @param {string} directory @returns {string[]}
 */
export function checkTexts(directory) {
  const files = readdirSync(directory)
    .filter((name) => name.endsWith('.json'))
    .sort();
  if (files.length === 0) return [`${directory}: catálogo sin archivos JSON`];
  const errors = [];
  const seen = new Map();
  for (const file of files) {
    const path = join(directory, file);
    /** @type {unknown} */
    const content = JSON.parse(readFileSync(path, 'utf8'));
    if (typeof content !== 'object' || content === null || Array.isArray(content)) {
      errors.push(`${file}: se esperaba un objeto plano de textos`);
      continue;
    }
    for (const [key, value] of Object.entries(content)) {
      if (seen.has(key)) errors.push(`${file}: ${key} repetida en ${String(seen.get(key))}`);
      seen.set(key, file);
      if (!/^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$/.test(key))
        errors.push(`${file}: clave inválida ${key}`);
      if (typeof value !== 'string' || value.trim() === '') {
        errors.push(`${file}: ${key} debe ser texto no vacío`);
      } else {
        errors.push(...textViolations(key, value).map((error) => `${file}: ${error}`));
      }
    }
  }
  return errors;
}
