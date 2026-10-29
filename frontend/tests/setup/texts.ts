/**
 * Las pruebas pintan pantallas sin el enrutador, que es quien carga los textos de cada pantalla con
 * su ruta (VIG-364): aquí se cargan todos antes de la primera prueba.
 */
import { loadAllTexts } from '@/texts';

await loadAllTexts();
