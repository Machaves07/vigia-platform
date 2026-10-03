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
