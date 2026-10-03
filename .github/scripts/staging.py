"""Pasos de AWS de los flujos de vigia-platform que no son ``cdk`` (TASK-151).

Solo biblioteca estándar y la CLI ``aws`` del runner, con las credenciales de
``aws_federation.py``. Órdenes:

- ``run-task``: lanza ``vigia-migrate`` o ``vigia-admin`` como tarea puntual en el despliegue con
  las salidas de ``vigia-compute`` (clúster, definición, subredes y grupo), espera a que pare y
  falla si el contenedor no termina con 0 (pasos 6 y 7 del orden corregido del primer despliegue,
  nota U02-H-01).
- ``output``: una salida de ``vigia-compute`` del despliegue (``NodeTrustStoreArn``…).
- ``residue``: tras ``cdk destroy`` de ``staging-<n>``, ningún recurso con la etiqueta
  ``environment=staging-<n>`` salvo claves KMS con borrado programado, y ninguna pila
  ``vigia-*-staging-<n>`` (D-8: «sin residuos»).
- ``orphans``: entornos ``staging-<n>`` con alguna pila de más de ``--hours`` horas (barrido
  nocturno de §2.1: 6 h).

Código 0 si todo va bien; 1 si una comprobación falla.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Any

Aws = Callable[..., Any]

STAGING = re.compile(r"^staging-[1-9][0-9]{0,8}$")
STAGING_STACK = re.compile(r"^vigia-[a-z]+-(staging-[1-9][0-9]{0,8})$")
CONTAINERS = {"migrate": "migrate", "admin": "admin"}
DEFINITIONS = {"migrate": "MigrateTaskDefinition", "admin": "AdminTaskDefinition"}
WAIT_ROUNDS = 6  # ``aws ecs wait tasks-stopped`` espera 10 min por ronda: 1 h en total
LIVE_STACK_STATES = frozenset(
    {
        "CREATE_IN_PROGRESS",
        "CREATE_FAILED",
        "CREATE_COMPLETE",
        "ROLLBACK_IN_PROGRESS",
        "ROLLBACK_FAILED",
        "ROLLBACK_COMPLETE",
        "DELETE_IN_PROGRESS",
        "DELETE_FAILED",
        "UPDATE_IN_PROGRESS",
        "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS",
        "UPDATE_COMPLETE",
        "UPDATE_FAILED",
        "UPDATE_ROLLBACK_IN_PROGRESS",
        "UPDATE_ROLLBACK_FAILED",
        "UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS",
        "UPDATE_ROLLBACK_COMPLETE",
        "IMPORT_IN_PROGRESS",
        "IMPORT_COMPLETE",
        "IMPORT_ROLLBACK_IN_PROGRESS",
        "IMPORT_ROLLBACK_FAILED",
        "IMPORT_ROLLBACK_COMPLETE",
    }
)


def aws_cli(*arguments: str) -> Any:
    completed = subprocess.run(  # noqa: S603 - argumentos propios, sin shell
        ["aws", *arguments, "--output", "json"],  # noqa: S607 - CLI del runner
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout) if completed.stdout.strip() else {}


def compute_stack(environment: str) -> str:
    return "vigia-compute" if environment == "pilot" else f"vigia-compute-{environment}"


def outputs(aws: Aws, environment: str) -> dict[str, str]:
    answer = aws("cloudformation", "describe-stacks", "--stack-name", compute_stack(environment))
    (stack,) = answer["Stacks"]
    return {item["OutputKey"]: item["OutputValue"] for item in stack.get("Outputs", [])}


def run_task(
    aws: Aws, environment: str, task: str, command: Sequence[str], started_by: str
) -> tuple[bool, str]:
    values = outputs(aws, environment)
    network = {
        "awsvpcConfiguration": {
            "subnets": values["AppSubnets"].split(","),
            "securityGroups": [values["OneOffSecurityGroup"]],
            "assignPublicIp": "DISABLED",
        }
    }
    arguments = [
        "ecs", "run-task",
        "--cluster", values["ClusterName"],
        "--task-definition", values[DEFINITIONS[task]],
        "--launch-type", "FARGATE",
        "--network-configuration", json.dumps(network),
        "--started-by", started_by[:36],
    ]  # fmt: skip
    if command:
        overrides = {"containerOverrides": [{"name": CONTAINERS[task], "command": list(command)}]}
        arguments += ["--overrides", json.dumps(overrides)]
    started = aws(*arguments)
    if started.get("failures") or not started.get("tasks"):
        return False, f"run-task rechazado: {started.get('failures')}"
    task_arn = started["tasks"][0]["taskArn"]
    for _ in range(WAIT_ROUNDS):
        try:
            aws(
                "ecs",
                "wait",
                "tasks-stopped",
                "--cluster",
                values["ClusterName"],
                "--tasks",
                task_arn,
            )
            break
        except subprocess.CalledProcessError:
            continue
    described = aws(
        "ecs", "describe-tasks", "--cluster", values["ClusterName"], "--tasks", task_arn
    )
    (detail,) = described["tasks"]
    if detail.get("lastStatus") != "STOPPED":
        return False, f"{task} no paró en {WAIT_ROUNDS * 10} minutos ({task_arn})"
    container = next(c for c in detail["containers"] if c["name"] == CONTAINERS[task])
    code = container.get("exitCode")
    if code != 0:
        reason = container.get("reason") or detail.get("stoppedReason")
        return False, f"{task} terminó con {code}: {reason} ({task_arn})"
    return True, f"{task} terminó con 0 ({task_arn})"


def residue(aws: Aws, environment: str) -> list[str]:
    """Lo que queda de ``staging-<n>`` tras destruirlo; vacío si no queda nada vivo."""
    problems = []
    tagged = aws(
        "resourcegroupstaggingapi", "get-resources",
        "--tag-filters", f"Key=environment,Values={environment}",
    )  # fmt: skip
    for mapping in tagged.get("ResourceTagMappingList", []):
        arn = mapping["ResourceARN"]
        if arn.split(":")[2] == "kms":
            state = aws("kms", "describe-key", "--key-id", arn)["KeyMetadata"]["KeyState"]
            if state != "PendingDeletion":
                problems.append(f"clave KMS sin borrado programado ({state}): {arn}")
            continue
        problems.append(f"recurso vivo: {arn}")
    stacks = aws(
        "cloudformation", "list-stacks", "--stack-status-filter", *sorted(LIVE_STACK_STATES)
    )
    for summary in stacks.get("StackSummaries", []):
        match = STAGING_STACK.match(summary["StackName"])
        if match and match[1] == environment:
            problems.append(f"pila sin destruir: {summary['StackName']} ({summary['StackStatus']})")
    return problems


def orphans(aws: Aws, now: datetime, hours: int) -> list[str]:
    """Entornos ``staging-<n>`` con alguna pila viva creada hace más de ``hours`` horas."""
    stacks = aws(
        "cloudformation", "list-stacks", "--stack-status-filter", *sorted(LIVE_STACK_STATES)
    )
    found = set()
    for summary in stacks.get("StackSummaries", []):
        match = STAGING_STACK.match(summary["StackName"])
        if not match:
            continue
        created = datetime.fromisoformat(summary["CreationTime"].replace("Z", "+00:00"))
        if now - created > timedelta(hours=hours):
            found.add(match[1])
    return sorted(found, key=lambda name: int(name.split("-")[1]))


def _environment(value: str, *, staging_only: bool = False) -> str:
    if (value != "pilot" or staging_only) and not STAGING.match(value):
        raise argparse.ArgumentTypeError("'staging-<n>'" + ("" if staging_only else " o 'pilot'"))
    return value


def main(argv: Sequence[str] | None = None, aws: Aws = aws_cli) -> int:
    parser = argparse.ArgumentParser(description="Pasos de AWS de los flujos (TASK-151).")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run-task")
    run.add_argument("--environment", required=True, type=_environment)
    run.add_argument("--task", required=True, choices=sorted(CONTAINERS))
    run.add_argument("--started-by", default="vigia-platform")
    run.add_argument("arguments", nargs="*", help="orden del contenedor (tras --)")
    out = commands.add_parser("output")
    out.add_argument("--environment", required=True, type=_environment)
    out.add_argument("--key", required=True)
    left = commands.add_parser("residue")
    left.add_argument(
        "--environment", required=True, type=lambda v: _environment(v, staging_only=True)
    )
    old = commands.add_parser("orphans")
    old.add_argument("--now", required=True, type=datetime.fromisoformat)
    old.add_argument("--hours", type=int, default=6)
    args = parser.parse_args(argv)

    if args.command == "run-task":
        ok, message = run_task(aws, args.environment, args.task, args.arguments, args.started_by)
        print(message)
        return 0 if ok else 1
    if args.command == "output":
        print(outputs(aws, args.environment)[args.key])
        return 0
    if args.command == "residue":
        problems = residue(aws, args.environment)
        for problem in problems:
            print(problem)
        if not problems:
            print(f"{args.environment}: sin residuos (claves KMS con borrado programado)")
        return 1 if problems else 0
    print(" ".join(orphans(aws, args.now, args.hours)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
