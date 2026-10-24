/**
 * Tabla de pantallas (LC-APP-03): los dieciséis `ScreenDescriptor` en el orden fijo de la
 * navegación. **Solo VIG-290 edita este archivo**: cada tarea de pantalla sustituye los relevos de
 * sus grupos en `src/pages/scrNN/<grupo>/` y, si precisa una sección, la línea de su grupo en
 * `src/pages/scrNN/descriptor.ts`.
 *
 * `GET /audit/entries` (`audit.read`) no tiene pantalla en el piloto (nota (4) de
 * `logical-components.md` §8).
 */
import type { ScreenDescriptor } from '@/app/descriptor';
import { descriptor as scr01 } from '@/pages/scr01/descriptor';
import { descriptor as scr02 } from '@/pages/scr02/descriptor';
import { descriptor as scr03 } from '@/pages/scr03/descriptor';
import { descriptor as scr04 } from '@/pages/scr04/descriptor';
import { descriptor as scr05 } from '@/pages/scr05/descriptor';
import { descriptor as scr06 } from '@/pages/scr06/descriptor';
import { descriptor as scr07 } from '@/pages/scr07/descriptor';
import { descriptor as scr08 } from '@/pages/scr08/descriptor';
import { descriptor as scr09 } from '@/pages/scr09/descriptor';
import { descriptor as scr10 } from '@/pages/scr10/descriptor';
import { descriptor as scr11 } from '@/pages/scr11/descriptor';
import { descriptor as scr12 } from '@/pages/scr12/descriptor';
import { descriptor as scr13 } from '@/pages/scr13/descriptor';
import { descriptor as scr14 } from '@/pages/scr14/descriptor';
import { descriptor as scr15 } from '@/pages/scr15/descriptor';
import { descriptor as scr16 } from '@/pages/scr16/descriptor';

export const SCREENS: readonly ScreenDescriptor[] = Object.freeze([
  scr01,
  scr02,
  scr03,
  scr04,
  scr05,
  scr06,
  scr07,
  scr08,
  scr09,
  scr10,
  scr11,
  scr12,
  scr13,
  scr14,
  scr15,
  scr16,
]);
