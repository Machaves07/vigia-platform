# Runbook · Traslado de la pila `vigia-datasets` a `vigia-platform`

**Unidad**: U-02 · **Tarea**: TASK-150 (VIG-28) · **Código**: `infra/stacks/datasets.py` · **Ejecuta**: el dueño del producto, a mano, en el primer despliegue (TASK-154, VIG-98).
**Diseño**: `plataforma-nucleo/infrastructure-design/deployment-architecture.md` §5 y §8; `docs/decisions/ADR-002-ubicacion-pila-vigia-datasets.md`; adenda A-47 (U-02 se cierra sin AWS: este runbook se ejecuta cuando VIG-98 se retome).

> **Regla de bloqueo (P1)**: mientras la tabla de `shared-infrastructure.md` §2 tenga algún `TBD`, no se ejecuta ni `cdk diff` ni `cdk deploy` contra la cuenta.

---

## 0. Qué cambia respecto a la pila de U-01

La pila conserva el nombre `vigia-datasets` y los identificadores lógicos de sus depósitos y de sus políticas. `infra/tests/test_datasets_logical_ids.py` lo comprueba contra la síntesis de `vigia-contracts/infra` (commit `467ebf3`), guardada en `infra/tests/fixtures/vigia-contracts-datasets.template.json`.

| Identificador lógico | Recurso | Cambio |
|---|---|---|
| `DatasetsBucket` | `vigia-datasets-<cuenta>-us-east-1` | Solo etiquetas: `unit=U-02` y `environment=pilot` (conserva `data=anonymized`). Sin reemplazo |
| `DatasetsBucketPolicy` | Política del depósito del conjunto | Ninguno |
| `LogsBucket` | `vigia-logs-<cuenta>-us-east-1` | Solo etiquetas, como el anterior. Sin reemplazo |
| `LogsBucketPolicy` | Política del depósito de registros | Conserva sus dos sentencias y añade la entrega de registros de acceso de `pilot` compartido: `s3/evidence/`, `s3/archive/`, `s3/edge/` y `s3/drill/` del servicio de registro de S3, cada una limitada a su depósito de `vigia-data`; `alb/app/` y `alb/nodes/` de Elastic Load Balancing (`127311923021`) y de `delivery.logs.amazonaws.com` (contingencia de R2) |
| `MonthlyBudget`, `StagingBudget` | Presupuestos `vigia-monthly` y `vigia-staging` | **No se trasladan**: los declara `vigia-foundation` (TASK-145) con el mismo nombre y avisan por `vigia-alerts`. Un nombre de presupuesto es único en la cuenta: las dos pilas no pueden tener los dos |
| Parámetro `BudgetAlertEmail` | Correo de los presupuestos | Desaparece con los presupuestos |

La pila solo se sintetiza con `environment=pilot` e `instance=shared` (D-8): ni `staging-<n>` ni una instancia dedicada la tienen.

## 1. Preparar el PC

Requisitos (`deployment-architecture.md` §9): `uv`, Node.js ≥ 22 y AWS CLI v2 con un perfil de la identidad administrativa con segundo factor.

```text
cd "<vigia-platform>\infra"
uv sync --frozen
uv run pytest -q
npx aws-cdk synth --quiet
```

Las pruebas deben pasar antes de seguir; entre ellas, `tests/test_datasets_logical_ids.py`.

## 2. Revisar el cambio (`cdk diff vigia-datasets`)

```text
cd "<vigia-platform>\infra"
aws sts get-caller-identity --profile <perfil>
npx aws-cdk diff vigia-datasets --profile <perfil>
```

El contexto por defecto de `cdk.json` ya es `environment=pilot` e `instance=shared`. Adjunta la salida a la solicitud de integración del despliegue (`shared-infrastructure.md` §4). Lo esperado depende de si la pila ya existe en la cuenta:

**A. La pila no existe** (estado al 2026-10-02: U-01 nunca la desplegó, P1 pendiente). El `diff` muestra la creación de los cuatro recursos de la tabla (`[+]`), sin roles ni políticas IAM.

**B. La pila ya existe, desplegada desde `vigia-contracts`.** El `diff` solo puede mostrar:

- `[~]` en `DatasetsBucket` y `LogsBucket`, solo en `Tags`;
- `[~]` en `LogsBucketPolicy`, con las sentencias añadidas;
- `[-]` en `MonthlyBudget` y `StagingBudget`, y la desaparición del parámetro `BudgetAlertEmail`;
- cambios en la descripción de la pila y en `CDKMetadata`.

**Detente** si aparece cualquier otra cosa, en particular:

- un depósito o su política con `replace`, `may be replaced` o `[-]`;
- un identificador lógico de depósito distinto de `DatasetsBucket` o `LogsBucket`.

En ese caso los identificadores no coinciden: no se despliega, y se corrige `infra/stacks/datasets.py` hasta que su prueba y el `diff` lo confirmen.

## 3. Desplegar, antes que `vigia-foundation` y `vigia-data`

`vigia-datasets` no depende de ninguna pila. En el orden de `deployment-architecture.md` §5 va en el paso 12, porque el diseño suponía que U-01 ya la había desplegado. Aquí se despliega **antes del paso 1**, en los dos casos:

- en `pilot` compartido, `vigia-data` y `vigia-edge` envían sus registros de acceso al `vigia-logs` de esta pila, que debe existir y conceder la entrega antes;
- en el caso B, desplegarla primero borra los presupuestos de la pila de U-01, y `vigia-foundation` puede crearlos después con el mismo nombre. En el orden inverso, `vigia-foundation` falla porque el presupuesto ya existe.

```text
npx aws-cdk deploy vigia-datasets --profile <perfil>
```

Ya no lleva `--parameters BudgetAlertEmail=...`.

Después:

1. Comprueba los depósitos:
   ```text
   aws s3api get-object-lock-configuration --bucket vigia-datasets-<cuenta>-us-east-1 --profile <perfil>
   aws s3api get-bucket-logging --bucket vigia-datasets-<cuenta>-us-east-1 --profile <perfil>
   aws s3api get-bucket-policy --bucket vigia-logs-<cuenta>-us-east-1 --profile <perfil>
   ```
   Lo esperado:
   - bloqueo `GOVERNANCE` de 365 días;
   - registro de acceso hacia `vigia-logs-<cuenta>-us-east-1` con prefijo `s3/datasets/`;
   - en la política de `vigia-logs`, los prefijos de la tabla §0.
2. En el caso B, `aws budgets describe-budgets --account-id <cuenta>` no lista `vigia-monthly` ni `vigia-staging` hasta desplegar `vigia-foundation`.
3. Sigue con el paso 1 de §5: `vigia-foundation`.

## 4. Cierre del traslado (§8, pasos 4 y 5)

1. La solicitud de integración de `vigia-contracts` que retira `infra/` y su flujo, y quita `infra/` de su `CODEOWNERS`, la prepara la sesión de control y la fusiona el dueño (A-47). No se fusiona antes del despliegue de este runbook si la pila ya existía (caso B): hasta entonces es la única forma de cambiarla.
2. Registra en `shared-infrastructure.md` §1 (fila «Pila `vigia-datasets`») y §6 la fecha, el caso (A o B) y la salida del `diff`.
