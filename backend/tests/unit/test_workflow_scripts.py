"""Scripts de los flujos en ``.github/scripts/`` (TASK-151).

- ``aws_federation.py``: solo el rol ``vigia-deploy``, token OIDC con audiencia de STS y
  credenciales enmascaradas antes de escribirlas (SECURITY-10: sin claves de larga duración).
- ``github_app_token.py``: JWT RS256 de ``vigia-release`` verificable con su clave pública y token
  limitado al repositorio y a ``contents: write``.
- ``staging.py``: tarea puntual que falla si el contenedor no termina con 0, residuos de un
  ``staging-<n>`` destruido (D-8) y huérfanos de más de 6 horas.

Sin AWS ni GitHub: la CLI, la API y el servicio OIDC son dobles.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

SCRIPTS = Path(__file__).resolve().parents[3] / ".github" / "scripts"


def _module(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"workflow_{name}", SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


federation = _module("aws_federation")
app_token = _module("github_app_token")
staging = _module("staging")

CREDENTIALS = {
    "AccessKeyId": "ASIAEXAMPLE",
    "SecretAccessKey": "secreto-temporal",
    "SessionToken": "sesion-temporal",
    "Expiration": "2026-10-03T13:00:00Z",
}


# --- aws_federation.py ----------------------------------------------------------------------


def test_credentials_are_masked_before_they_reach_github_env(tmp_path: Path) -> None:
    env_file = tmp_path / "env"
    printed: list[str] = []
    federation.export(CREDENTIALS, env_file, printed.append)
    masks = [line for line in printed if line.startswith("::add-mask::")]
    assert len(masks) == 3
    for name in ("AccessKeyId", "SecretAccessKey", "SessionToken"):
        assert f"::add-mask::{CREDENTIALS[name]}\n" in masks
    lines = env_file.read_text(encoding="utf-8").splitlines()
    assert "AWS_SESSION_TOKEN=sesion-temporal" in lines and "AWS_REGION=us-east-1" in lines


def test_the_token_is_requested_with_the_sts_audience(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class Answer:
        def __enter__(self) -> Answer:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"value": "token-oidc"}'

    def urlopen(request: Any, timeout: float) -> Answer:
        seen["url"], seen["auth"] = request.full_url, request.headers["Authorization"]
        return Answer()

    monkeypatch.setattr(federation.urllib.request, "urlopen", urlopen)
    environ = {
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://runner.example/token?api-version=2.0",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "portador",
    }
    assert federation.request_token(environ) == "token-oidc"
    assert seen["url"].endswith("?api-version=2.0&audience=sts.amazonaws.com")
    assert seen["auth"] == "Bearer portador"


def test_without_id_token_permission_the_federation_stops() -> None:
    with pytest.raises(SystemExit):
        federation.request_token({})


@pytest.mark.parametrize(
    "role",
    [
        "arn:aws:iam::123456789012:role/Admin",
        "arn:aws:iam::123456789012:role/vigia-deploy/../Admin",
        "arn:aws:iam::12345:role/vigia-deploy",
        "arn:aws:iam::123456789012:user/vigia-deploy",
    ],
)
def test_only_the_vigia_deploy_role_can_be_assumed(role: str) -> None:
    with pytest.raises(SystemExit) as raised:
        federation.main(["--role-arn", role])
    assert raised.value.code == 2


def test_assume_uses_one_hour_web_identity_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        return subprocess.CompletedProcess(command, 0, json.dumps({"Credentials": CREDENTIALS}), "")

    monkeypatch.setattr(federation.subprocess, "run", run)
    role = "arn:aws:iam::123456789012:role/vigia-deploy"
    assert federation.assume(role, "token", "gh-1-job") == CREDENTIALS
    (command,) = seen
    assert command[:3] == ["aws", "sts", "assume-role-with-web-identity"]
    assert command[command.index("--duration-seconds") + 1] == "3600"
    assert command[command.index("--role-arn") + 1] == role


# --- github_app_token.py --------------------------------------------------------------------


def _decode(part: str) -> Any:
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def _key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def test_the_app_jwt_is_rs256_and_verifies_with_the_public_key() -> None:
    key = _key()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    token = app_token.jwt("12345", pem, now=1_000_000)
    header, payload, signature = token.split(".")
    assert _decode(header) == {"alg": "RS256", "typ": "JWT"}
    assert _decode(payload) == {"iat": 999_940, "exp": 1_000_540, "iss": "12345"}
    raw = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    key.public_key().verify(
        raw, f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256()
    )


def test_the_installation_token_is_scoped_to_the_repository_and_contents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, object | None]] = []

    def call(method: str, path: str, bearer: str, body: object | None = None) -> Any:
        calls.append((method, path, body))
        return {"id": 77} if method == "GET" else {"token": "ghs_temporal"}

    monkeypatch.setattr(app_token, "jwt", lambda *_: "jwt")
    environ = {
        "GITHUB_REPOSITORY": "Machaves07/vigia-platform",
        "VIGIA_RELEASE_APP_ID": "1",
        "VIGIA_RELEASE_PRIVATE_KEY": "pem",
    }
    assert app_token.installation_token(environ, call=call, now=lambda: 0.0) == "ghs_temporal"
    assert calls == [
        ("GET", "/repos/Machaves07/vigia-platform/installation", None),
        (
            "POST",
            "/app/installations/77/access_tokens",
            {"repositories": ["vigia-platform"], "permissions": {"contents": "write"}},
        ),
    ]


# --- staging.py -----------------------------------------------------------------------------

OUTPUTS = {
    "Stacks": [
        {
            "Outputs": [
                {"OutputKey": "ClusterName", "OutputValue": "vigia-staging-7"},
                {"OutputKey": "MigrateTaskDefinition", "OutputValue": "arn:td/migrate:1"},
                {"OutputKey": "AdminTaskDefinition", "OutputValue": "arn:td/admin:1"},
                {"OutputKey": "OneOffSecurityGroup", "OutputValue": "sg-1"},
                {"OutputKey": "AppSubnets", "OutputValue": "subnet-a,subnet-b"},
            ]
        }
    ]
}


class FakeAws:
    def __init__(self, exit_code: int | None = 0, stopped: bool = True, **answers: Any) -> None:
        self.exit_code, self.stopped, self.answers = exit_code, stopped, answers
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *arguments: str) -> Any:
        self.calls.append(arguments)
        key = f"{arguments[0]} {arguments[1]}"
        if key == "cloudformation describe-stacks":
            return OUTPUTS
        if key == "ecs run-task":
            definition = arguments[arguments.index("--task-definition") + 1]
            self.container = "migrate" if "migrate" in definition else "admin"
            return {"tasks": [{"taskArn": "arn:task/1"}], "failures": []}
        if key == "ecs wait":
            return {}
        if key == "ecs describe-tasks":
            container: dict[str, Any] = {"name": self.container, "exitCode": self.exit_code}
            status = "STOPPED" if self.stopped else "RUNNING"
            return {"tasks": [{"lastStatus": status, "containers": [container]}]}
        return self.answers[key]


def test_a_one_off_task_succeeds_only_with_exit_code_zero() -> None:
    aws = FakeAws()
    ok, message = staging.run_task(aws, "staging-7", "migrate", [], "release-1")
    assert ok, message
    run = next(c for c in aws.calls if c[:2] == ("ecs", "run-task"))
    assert run[run.index("--task-definition") + 1] == "arn:td/migrate:1"
    network = json.loads(run[run.index("--network-configuration") + 1])
    assert network["awsvpcConfiguration"] == {
        "subnets": ["subnet-a", "subnet-b"],
        "securityGroups": ["sg-1"],
        "assignPublicIp": "DISABLED",
    }
    assert "--overrides" not in run
    assert (
        "cloudformation",
        "describe-stacks",
        "--stack-name",
        "vigia-compute-staging-7",
    ) in aws.calls


def test_the_admin_command_goes_as_a_container_override() -> None:
    aws = FakeAws()
    command = ["vigia-admin", "bootstrap", "--yes"]
    ok, _ = staging.run_task(aws, "staging-7", "admin", command, "release-1")
    assert ok
    run = next(c for c in aws.calls if c[:2] == ("ecs", "run-task"))
    overrides = json.loads(run[run.index("--overrides") + 1])
    assert overrides == {"containerOverrides": [{"name": "admin", "command": command}]}


@pytest.mark.parametrize(("exit_code", "stopped"), [(1, True), (None, True), (0, False)])
def test_a_failed_or_unfinished_task_fails(exit_code: int | None, stopped: bool) -> None:
    ok, _ = staging.run_task(FakeAws(exit_code, stopped), "staging-7", "migrate", [], "r")
    assert not ok


def test_pilot_reads_the_permanent_compute_stack() -> None:
    assert staging.compute_stack("pilot") == "vigia-compute"
    assert staging.compute_stack("staging-12") == "vigia-compute-staging-12"


def _residue_aws(resources: list[str], key_state: str, stacks: list[dict[str, str]]) -> FakeAws:
    return FakeAws(
        **{
            "resourcegroupstaggingapi get-resources": {
                "ResourceTagMappingList": [{"ResourceARN": arn} for arn in resources]
            },
            "kms describe-key": {"KeyMetadata": {"KeyState": key_state}},
            "cloudformation list-stacks": {"StackSummaries": stacks},
        }
    )


KEY = "arn:aws:kms:us-east-1:111:key/abc"


def test_a_clean_staging_leaves_only_keys_pending_deletion() -> None:
    aws = _residue_aws([KEY], "PendingDeletion", [])
    assert staging.residue(aws, "staging-7") == []
    tags = next(c for c in aws.calls if c[0] == "resourcegroupstaggingapi")
    assert "Key=environment,Values=staging-7" in tags


@pytest.mark.parametrize(
    ("resources", "state", "stacks"),
    [
        ([KEY], "Enabled", []),
        (["arn:aws:s3:::vigia-evidence-staging-7-111-us-east-1"], "PendingDeletion", []),
        (
            [],
            "PendingDeletion",
            [{"StackName": "vigia-data-staging-7", "StackStatus": "DELETE_FAILED"}],
        ),
    ],
)
def test_any_live_resource_key_or_stack_is_residue(
    resources: list[str], state: str, stacks: list[dict[str, str]]
) -> None:
    assert staging.residue(_residue_aws(resources, state, stacks), "staging-7")


def test_stacks_of_other_stagings_are_not_residue() -> None:
    stacks = [
        {"StackName": "vigia-data-staging-70", "StackStatus": "CREATE_COMPLETE"},
        {"StackName": "vigia-data", "StackStatus": "CREATE_COMPLETE"},
    ]
    assert staging.residue(_residue_aws([], "PendingDeletion", stacks), "staging-7") == []


def test_orphans_are_stagings_with_a_stack_older_than_the_limit() -> None:
    now = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
    stacks = [
        {"StackName": "vigia-compute-staging-9", "CreationTime": "2026-10-03T05:59:00Z"},
        {"StackName": "vigia-data-staging-10", "CreationTime": "2026-10-03T06:01:00Z"},
        {"StackName": "vigia-data-staging-2", "CreationTime": "2026-10-02T12:00:00+00:00"},
        {"StackName": "vigia-data", "CreationTime": "2026-01-01T00:00:00Z"},
    ]
    aws = FakeAws(**{"cloudformation list-stacks": {"StackSummaries": stacks}})
    assert staging.orphans(aws, now, 6) == ["staging-2", "staging-9"]


@pytest.mark.parametrize(
    "arguments",
    [
        ["residue", "--environment", "pilot"],
        ["residue", "--environment", "staging-01"],
        ["run-task", "--environment", "prod", "--task", "migrate"],
        ["run-task", "--environment", "pilot", "--task", "worker"],
    ],
)
def test_the_command_rejects_other_environments_and_tasks(arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        staging.main(arguments, aws=FakeAws())
    assert raised.value.code == 2
