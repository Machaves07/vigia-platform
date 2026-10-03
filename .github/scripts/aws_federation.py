"""Credenciales temporales de ``vigia-deploy`` por federación de identidad de GitHub (TASK-151).

deployment-architecture §3.1 y SECURITY-10: ningún trabajo usa claves de AWS de larga duración.
Pide el token OIDC del trabajo (audiencia ``sts.amazonaws.com``; exige ``permissions: id-token:
write`` y, por la confianza del rol, ``environment: pilot`` o ``staging``), lo cambia por
credenciales de una hora con ``aws sts assume-role-with-web-identity`` (llamada sin firmar) y las
deja enmascaradas en ``$GITHUB_ENV`` para los pasos siguientes.

Sustituye a ``aws-actions/configure-aws-credentials`` con la biblioteca estándar y la CLI ``aws``
del runner: una acción de terceros menos con acceso a las credenciales. Un trabajo largo (el
``staging`` del release) lo vuelve a llamar antes de cada fase, con un token nuevo.

Uso: ``python3 .github/scripts/aws_federation.py --role-arn <ARN de vigia-deploy>``
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

AUDIENCE = "sts.amazonaws.com"
REGION = "us-east-1"
DURATION_SECONDS = 3600
_ROLE_ARN = re.compile(r"^arn:aws:iam::[0-9]{12}:role/vigia-deploy(?:-[a-z0-9-]+)?$")


def request_token(environ: Mapping[str, str]) -> str:
    """Token OIDC del trabajo desde el servicio del runner."""
    url = environ.get("ACTIONS_ID_TOKEN_REQUEST_URL")
    bearer = environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if not url or not bearer:
        raise SystemExit(
            "Sin token OIDC: el trabajo necesita 'permissions: id-token: write' y un entorno."
        )
    separator = "&" if urllib.parse.urlparse(url).query else "?"
    request = urllib.request.Request(  # noqa: S310 - URL del propio runner
        f"{url}{separator}audience={urllib.parse.quote(AUDIENCE)}",
        headers={"Authorization": f"Bearer {bearer}"},
    )
    with urllib.request.urlopen(request, timeout=30) as answer:  # noqa: S310
        return str(json.load(answer)["value"])


def assume(role_arn: str, token: str, session: str) -> dict[str, str]:
    completed = subprocess.run(  # noqa: S603 - argumentos propios, sin shell
        [  # noqa: S607 - CLI del runner
            "aws", "sts", "assume-role-with-web-identity",
            "--role-arn", role_arn,
            "--role-session-name", session,
            "--web-identity-token", token,
            "--duration-seconds", str(DURATION_SECONDS),
            "--region", REGION,
            "--output", "json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )  # fmt: skip
    credentials: dict[str, str] = json.loads(completed.stdout)["Credentials"]
    return credentials


def export(credentials: Mapping[str, str], env_file: Path, out: Callable[[str], None]) -> None:
    """Enmascara los valores y los añade a ``$GITHUB_ENV``."""
    for name in ("AccessKeyId", "SecretAccessKey", "SessionToken"):
        out(f"::add-mask::{credentials[name]}\n")
    with env_file.open("a", encoding="utf-8") as handle:
        handle.write(f"AWS_ACCESS_KEY_ID={credentials['AccessKeyId']}\n")
        handle.write(f"AWS_SECRET_ACCESS_KEY={credentials['SecretAccessKey']}\n")
        handle.write(f"AWS_SESSION_TOKEN={credentials['SessionToken']}\n")
        handle.write(f"AWS_REGION={REGION}\nAWS_DEFAULT_REGION={REGION}\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--role-arn", required=True)
    args = parser.parse_args(argv)
    if not _ROLE_ARN.match(args.role_arn):
        parser.error(f"rol fuera de la forma vigia-deploy: {args.role_arn}")
    environ = os.environ
    session = f"gh-{environ.get('GITHUB_RUN_ID', 'local')}-{environ.get('GITHUB_JOB', 'job')}"
    credentials = assume(args.role_arn, request_token(environ), session[:64])
    export(credentials, Path(environ["GITHUB_ENV"]), sys.stdout.write)
    sys.stdout.write(f"vigia-deploy asumido hasta {credentials['Expiration']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
