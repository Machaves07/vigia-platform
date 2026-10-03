"""Token de instalación de la aplicación ``vigia-release`` (TASK-151; A-40).

La etiqueta ``vX.Y.Z`` y el release los crea ``vigia-release``, no el ``GITHUB_TOKEN`` del flujo
(que es de solo lectura). Firma un JWT RS256 con la clave privada de la aplicación (``openssl``
del runner), pide la instalación del repositorio y un token de una hora limitado a ese
repositorio y a ``contents: write``, y lo deja enmascarado en ``$GITHUB_OUTPUT`` como ``token``.

Sustituye a ``actions/create-github-app-token`` con la biblioteca estándar: una acción de
terceros menos con acceso a la clave privada.

Entorno: ``VIGIA_RELEASE_APP_ID``, ``VIGIA_RELEASE_PRIVATE_KEY``, ``GITHUB_REPOSITORY``,
``GITHUB_OUTPUT``.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

API = "https://api.github.com"
PERMISSIONS = {"contents": "write"}


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def jwt(app_id: str, private_key: str, now: int) -> str:
    """JWT de la aplicación: válido 9 minutos, con 60 s de margen por la deriva del reloj."""
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = _b64(json.dumps({"iat": now - 60, "exp": now + 540, "iss": app_id}).encode())
    signing_input = f"{header}.{payload}".encode()
    with tempfile.TemporaryDirectory() as directory:
        key = Path(directory, "key.pem")
        key.touch(mode=0o600)
        key.write_text(private_key, encoding="utf-8")
        signature = subprocess.run(  # noqa: S603 - argumentos propios, sin shell
            ["openssl", "dgst", "-sha256", "-sign", str(key)],  # noqa: S607 - openssl del runner
            input=signing_input,
            check=True,
            capture_output=True,
        ).stdout
    return f"{header}.{payload}.{_b64(signature)}"


def _call(method: str, path: str, bearer: str, body: object | None = None) -> Any:
    request = urllib.request.Request(  # noqa: S310 - API de GitHub por HTTPS
        f"{API}{path}",
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={
            "Authorization": f"Bearer {bearer}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as answer:  # noqa: S310
        return json.load(answer)


def installation_token(
    environ: Mapping[str, str],
    call: Callable[..., Any] = _call,
    now: Callable[[], float] = time.time,  # noqa: TID251 - reloj del JWT, fuera del núcleo
) -> str:
    owner, repository = environ["GITHUB_REPOSITORY"].split("/", 1)
    app_jwt = jwt(environ["VIGIA_RELEASE_APP_ID"], environ["VIGIA_RELEASE_PRIVATE_KEY"], int(now()))
    installation = call("GET", f"/repos/{owner}/{repository}/installation", app_jwt)
    answer = call(
        "POST",
        f"/app/installations/{installation['id']}/access_tokens",
        app_jwt,
        {"repositories": [repository], "permissions": PERMISSIONS},
    )
    return str(answer["token"])


def main() -> int:
    token = installation_token(os.environ)
    sys.stdout.write(f"::add-mask::{token}\n")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as handle:
        handle.write(f"token={token}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
