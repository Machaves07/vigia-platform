/**
 * Límites de un campo de motivo leídos del esquema espejo generado (`openapi/bodies/*.json`,
 * `npm run generate`): la unidad dueña los fija y la interfaz nunca los copia (BR-APP-44). La
 * pantalla importa el esquema de su operación y nombra el campo; si el esquema no fija
 * `minLength` y `maxLength` para ese campo, es un error de programación, nunca un límite inventado.
 */
import type { FreeTextLimits } from '@/ports/validation';

/** Forma mínima de un archivo de `openapi/bodies/`. */
export interface GeneratedBodySchema {
  readonly operationId: string;
  readonly body: unknown;
}

function record(value: unknown): Readonly<Record<string, unknown>> | undefined {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

/** El esquema de cuerpo resuelto a su `$defs` raíz (`{$ref: '#/$defs/X', $defs: {X: …}}`). */
function rootObject(body: unknown): Readonly<Record<string, unknown>> | undefined {
  const schema = record(body);
  const ref = schema?.$ref;
  if (typeof ref === 'string' && ref.startsWith('#/$defs/')) {
    return record(record(schema?.$defs)?.[ref.slice('#/$defs/'.length)]);
  }
  return schema;
}

/** Un campo anulable (`anyOf: [{type: string, …}, {type: null}]`) aporta su rama de texto. */
function stringBranch(
  property: Readonly<Record<string, unknown>> | undefined,
): Readonly<Record<string, unknown>> | undefined {
  const branches = property?.anyOf;
  if (!Array.isArray(branches)) return property;
  const strings = branches.map(record).filter((branch) => branch?.type === 'string');
  return strings.length === 1 ? strings[0] : undefined;
}

export function reasonLimits(document: GeneratedBodySchema, field: string): FreeTextLimits {
  const property = stringBranch(record(record(rootObject(document.body)?.properties)?.[field]));
  const minLength = property?.minLength;
  const maxLength = property?.maxLength;
  if (
    property?.type !== 'string' ||
    typeof minLength !== 'number' ||
    typeof maxLength !== 'number'
  ) {
    throw new RangeError(
      `${document.operationId}: el campo «${field}» no fija minLength y maxLength en el esquema generado.`,
    );
  }
  return { minLength, maxLength };
}
