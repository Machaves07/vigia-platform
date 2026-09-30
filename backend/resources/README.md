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
