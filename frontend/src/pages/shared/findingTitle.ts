/** Nombre humano de un hallazgo (sistema visual v1): «<familia> en <zona>», sin identificadores. */
import { text } from '@/texts';
import { label } from '@/texts/labels';

/** «Coexistencia en Prensa 3»; sin la zona en la jerarquía, «en zona desconocida». */
export function findingTitle(family: string, zone: string | null): string {
  return `${label('predicate_family', family)} ${text('finding_title.in')} ${
    zone ?? text('finding_title.unknown_zone')
  }`;
}
