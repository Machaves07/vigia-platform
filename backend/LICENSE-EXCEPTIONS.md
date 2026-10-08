# Excepciones de licencia de vigia-platform

Registro que lee `tools/check_licenses.py` (trabajo «licencias» de `ci.yml`, TASK-143). La lista
permitida es la de NFR-CTR-26: MIT, BSD-2-Clause, BSD-3-Clause, Apache-2.0, ISC, PSF y MPL-2.0.
Toda otra licencia necesita una fila aquí, con su motivo y una fecha de revisión: una excepción
vencida vuelve a fallar la canalización.

- **Alcance `herramienta de CI`**: la excepción solo vale mientras el paquete no sea una dependencia
  de ejecución de la imagen (el cierre de `vigia-platform` sin grupos en `uv.lock`). Mismo criterio
  que `chardet` en la entrada A-43 de la adenda.
- **Alcance `ejecución`**: el paquete llega a la imagen.

| Id | Paquete | Licencia | Revisión | Alcance | Motivo |
|---|---|---|---|---|---|
| EX-01 | qrcode | LicenseRef-Proprietary | 2027-03-31 | ejecución | El `LICENSE` de qrcode 8.2 es BSD-3-Clause (Lincoln Loop) más el aviso MIT del código original (Kazuhiko Arase). El clasificador `Other/Proprietary License` alude a la marca «QR Code» de DENSO WAVE, no a una licencia del código. Seguimiento de VIG-18. |
| EX-02 | cffi | MIT-0 | 2027-03-31 | ejecución | MIT sin la cláusula de atribución: igual o más permisiva que MIT. Misma excepción que EX-02 de vigia-contracts (A-43). |
| EX-03 | numpy | 0BSD, Zlib, CC0-1.0 | 2027-03-31 | herramienta de CI | Solo en el grupo `dev`: lo importan los generadores del kit de conformidad de U-01. Misma excepción que EX-04 de vigia-contracts (A-43). El wheel de Linux incluye además `libgfortran` (GPL-3.0 con la excepción de GCC) y `libquadmath` (LGPL-2.1), que sus metadatos no declaran; no se distribuyen (seguimiento de VIG-46). |
| EX-04 | pillow | MIT-CMU | 2027-03-31 | ejecución | Dependencia de WeasyPrint (documento legible del acta, LC-GOB-08; A-53 y R-GOB-13). Pillow 12 declara `License-Expression: MIT-CMU`, la licencia HPND de PIL (aviso de Secret Labs y Fredrik Lundh): permisiva, sin copyleft; solo exige conservar el aviso. Revisada el 2026-10-08 (VIG-160). |
| EX-05 | pyphen | GNU General Public License v2 or later (GPLv2+), GNU Lesser General Public License v2 or later (LGPLv2+), Mozilla Public License 1.1 (MPL 1.1) | 2027-03-31 | ejecución | Dependencia de WeasyPrint para la división de palabras (A-53 y R-GOB-13). Pyphen 0.18 se ofrece bajo **cualquiera** de las tres licencias (GPL-2.0+, LGPL-2.1+ o MPL-1.1, como sus diccionarios de Hunspell), pero sus metadatos solo las declaran como clasificadores, y `check_licenses.py` exige todos los clasificadores. Vigía la usa bajo **MPL-1.1**: copyleft por archivo y paquete sin modificar. Revisada el 2026-10-08 (VIG-160). |
