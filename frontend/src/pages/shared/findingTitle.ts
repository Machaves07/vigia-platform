/** Nombre humano de un hallazgo (sistema visual v1): «<familia> en <zona>», sin identificadores. */
import { text } from '@/texts';
import { label } from '@/texts/labels';

function capitalized(value: string): string {
  return value.charAt(0).toLocaleUpperCase('es-CO') + value.slice(1);
}

/** «Coexistencia en Prensa 3»; sin la zona en la jerarquía, «en zona desconocida». */
export function findingTitle(family: string, zone: string | null): string {
  return `${capitalized(label('predicate_family', family))} ${text('finding_title.in')} ${
    zone ?? text('finding_title.unknown_zone')
  }`;
}
