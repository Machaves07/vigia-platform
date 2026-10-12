# Recursos de la imagen del backend

- `pwned-top100k.txt`: respaldo local de contraseñas filtradas (NFR-NUC-26, riesgo R8). Es el SHA-1 en
  hexadecimal en mayúsculas de las 100 000 contraseñas más filtradas, uno por línea y ordenados, sin
  ninguna contraseña en claro. **No se guarda en el repositorio**: se genera en cada construcción de la
  imagen con `tools/build_pwned_top100k.py` y se copia con `backend/`. Sin él,
  `identity.adapters.hibp.LocalBreachList.from_file()` lanza y el servicio no arranca.

  ```text
  cd backend && uv run python tools/build_pwned_top100k.py --source <volcado> --format hibp
  ```

  **Procedencia de los datos:**

  - `--format hibp` (preferida). La fuente es el volcado `<SHA-1>:<cuenta>`, ordenado por hash, que
    genera el descargador oficial de Pwned Passwords de Have I Been Pwned
    (`PwnedPasswordsDownloader`). Se lee en flujo y con memoria constante. Según la documentación de
    la API v3 de HIBP, Pwned Passwords no exige licencia ni atribución. Aun así, se cita aquí su
    origen: Have I Been Pwned, <https://haveibeenpwned.com/Passwords>.
  - `--format plaintext`. La fuente es una lista de contraseñas ordenada por frecuencia, por ejemplo
    las listas de 100 000 de SecLists (licencia MIT). Si se usa esa fuente, su aviso de licencia MIT
    se conserva junto al archivo generado en la imagen.

  Consulta la ayuda del guion (`--help`).

- `fonts/noto-sans/`: las únicas fuentes del documento legible del acta (TASK-217, A-53,
  NFR-GOB-32). `NotoSans-Regular.ttf` y `NotoSans-Bold.ttf` son Noto Sans 2.015, estáticas y sin
  hinting, de <https://github.com/notofonts/notofonts.github.io> (`fonts/NotoSans/unhinted/ttf/`),
  con licencia **SIL Open Font License 1.1**; `OFL.txt` es su licencia, de
  <https://github.com/notofonts/latin-greek-cyrillic>. Se guardan en el repositorio y llegan a la
  imagen con `backend/resources`: nunca se descargan en ejecución.
  - El `url_fetcher` del render (`catalog/adapters/rendering/record_document.py`) solo sirve estos
    dos archivos.
  - `tools/image_audit.py` exige que estén en `/app/resources/fonts/` con el mismo SHA-256 y que la
    imagen no tenga fuentes del sistema.
  - SHA-256: Regular `f3961a9cde016d41a4879aecda1474d3a36d6bf54fa0e4643de029cc2248b0e8`, Bold
    `87cb2d84472a7d66da659ee47b6cdb9552326e8c128245231f191b6ac72529d9`.
