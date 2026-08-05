# Aprovisionamiento de Data Encryption Keys (DEK)

Ceremonia de aprovisionamiento inicial de **Data Encryption Keys (DEK)** simétricas para el modelo de envelope encryption de la plataforma de datos, ejecutada vía GitHub Actions con autenticación **Service Principal + client secret** contra Azure Key Vault.

> Evento único de bootstrap por ambiente.

## Alcance

Este repositorio contiene el workflow y los scripts que:

1. Generan una DEK simétrica (AES-256) en memoria del runner.
2. La envuelven (wrap) usando la KEK del segmento correspondiente, vía Azure Key Vault.
3. Persisten únicamente el valor envuelto (ciphertext) como secreto en Key Vault, con metadata de trazabilidad.
4. Verifican la integridad del wrap desenvolviendo el secreto y validando su longitud, sin exponer el valor en claro.
5. Permiten validar credenciales y permisos de forma segura (`plan_only: true`) antes de ejecutar la ceremonia real.

La DEK en texto plano **nunca** persiste fuera de la memoria del runner, nunca se escribe a disco, nunca se pasa por `GITHUB_OUTPUT`, y nunca se imprime en logs.

## Estructura del repositorio

```
.
├── .github/
│   └── workflows/
│       └── dek-bootstrap.yml      # Workflow de la ceremonia
├── scripts/
│   ├── check_auth.py              # Validación de credenciales (modo plan_only, solo lectura)
│   ├── dek_bootstrap.py           # Generación + wrap + almacenamiento
│   └── verify_dek.py              # Verificación post-wrap (unwrap + validación de longitud)
└── README.md
```

## Modelo de autenticación

**Service Principal + client secret**, alineado al estándar organizacional vigente (`credicorp-internal`). Esto es una desviación intencional del principio "sin credenciales estáticas" evaluado originalmente con OIDC federation — ver la nota en el ADR de arquitectura sobre este trade-off.

Los scripts de Python construyen la credencial explícitamente con `ClientSecretCredential`, a partir de variables de entorno pasadas en cada step. Esto evita depender de una sesión de Azure CLI o de `DefaultAzureCredential()`, cuya cadena de fallback puede resolver una identidad distinta a la esperada si el runner tiene otros contextos disponibles.

```python
credential = ClientSecretCredential(
    tenant_id=os.environ["AZURE_TENANT_ID"],
    client_id=os.environ["AZURE_CLIENT_ID"],
    client_secret=os.environ["AZURE_CLIENT_SECRET"],
)
```

## Prerrequisitos

- KEK del ambiente ya provisionada en Key Vault.
- App Registration en Entra ID con **client secret** generado (Certificates & secrets → New client secret).
- Rol `Key Vault Crypto User` asignado al Service Principal sobre la KEK (permite `wrapKey`/`unwrapKey`).
- Rol `Key Vault Secrets Officer` asignado al Service Principal sobre el vault de almacenamiento de secretos (permite `setSecret`/`getSecret`). Namespace de permisos distinto al anterior — ambos roles son requeridos.
- Un GitHub team (ej. `gh-data-security`) cuyos miembros estén autorizados a disparar la ceremonia.
- Secrets y variables configurados (ver siguiente sección).

## Configuración de identidad

### Opción CLI

```bash
# 1. Crear App Registration
az ad app create --display-name "github-actions-dek-bootstrap"

# 2. Crear Service Principal asociado
az ad sp create --id <APP_ID>

# 3. Generar client secret
az ad app credential reset --id <APP_ID> --display-name "github-actions-dek-bootstrap-secret"
#    Copiar el valor del secret inmediatamente — no se puede recuperar después.

# 4. Asignar permisos de wrap/unwrap sobre la KEK
az role assignment create \
  --assignee <SP_OBJECT_ID> \
  --role "Key Vault Crypto User" \
  --scope <KEK_RESOURCE_ID>

# 5. Asignar permisos de gestión de secretos sobre el vault de almacenamiento
az role assignment create \
  --assignee <SP_OBJECT_ID> \
  --role "Key Vault Secrets Officer" \
  --scope <SECRET_VAULT_RESOURCE_ID>
```
> "github-actions-dek-bootstrap" y "github-actions-dek-bootstrap-secret" son nombres referenciales.

### Opción manual — Azure Portal

Alternativa a los comandos CLI, útil si no tienes acceso a Azure CLI o si otro equipo (ej. Seguridad/IAM) debe ejecutar estos pasos por segregación de funciones.

**1. Crear el App Registration**

- Entra ID → **App registrations** → **New registration**.
- Nombre: `github-actions-dek-bootstrap`.
- Supported account types: *Accounts in this organizational directory only* (single tenant).
- **Register**.
- En **Overview**, copiar el **Application (client) ID** → `AZURE_CLIENT_ID_SP`.

**2. Generar el client secret**

- En la misma app → **Certificates & secrets** → pestaña **Client secrets** → **New client secret**.
- Descripción y expiración (definir según política de rotación de la organización).
- **Add** → copiar el **Value** inmediatamente (no se vuelve a mostrar) → `AZURE_CLIENT_SECRET_SP`.

**3. Asignar el rol sobre la KEK**

- Ir al Key Vault que contiene la KEK → **Access control (IAM)** → **Add** → **Add role assignment**.
- Role: `Key Vault Crypto User`.
- Members: buscar y seleccionar `github-actions-dek-bootstrap`.
- **Review + assign**.
- Nota: el Portal generalmente asigna el rol a nivel de vault completo. Para scope granular sobre una key específica, usar el blade de la key individual (**Keys** → seleccionar la KEK → **Access control (IAM)**) si el SKU lo permite, o usar CLI.

**4. Asignar el rol sobre el vault de secretos**

- Repetir el paso anterior sobre el Key Vault donde se almacenará la DEK envuelta.
- Role: `Key Vault Secrets Officer`.

**5. Obtener Tenant ID y Subscription ID**

- **Tenant ID**: Entra ID → **Overview** → campo *Tenant ID*.
- **Subscription ID**: **Subscriptions** → seleccionar la suscripción correspondiente → campo *Subscription ID*.

> "github-actions-dek-bootstrap" es un nombre referencial.

## Configuración de secrets y variables en GitHub

**Settings → Secrets and variables → Actions**

| Nombre | Tipo | Valor |
|---|---|---|
| `AZURE_CLIENT_ID_SP` | Secret | Application (client) ID del App Registration |
| `AZURE_CLIENT_SECRET_SP` | Secret | Client secret generado en el paso anterior |
| `GH_PAT_READ_ORG` | Secret | Personal Access Token con permiso de lectura sobre membresía de teams de la organización |
| `AZURE_TENANT_ID` | Variable | Tenant ID de Entra ID |

### Control gate por membresía de equipo

El workflow implementa su propio gate de autorización mediante un job `validation` que verifica si `github.actor` pertenece a un GitHub team autorizado (ej. `gh-data-security`), usando la action `tspascoal/get-user-teams-membership@v2`. El job de la ceremonia solo se ejecuta si esa condición se cumple:

```yaml
if: github.ref == 'refs/heads/main' && contains(needs.validation.outputs.teams, 'gh-data-security')
```

Ajustar `gh-data-security` al nombre real del team autorizado en la organización.

## Ejecución

1. Ve a la pestaña **Actions** → `DEK Bootstrap Ceremony - Production` → **Run workflow**.
2. Ingresa `kek_vault_url‎`, `kek_name‎`, `secret_vault_url‎` y 'secret_name'.‎
3. Opcional: marca `plan_only: true` para solo validar credenciales y permisos, sin generar ni almacenar ninguna DEK (ver siguiente sección).
4. El job `validation` verifica la membresía de equipo del actor.
5. Si el gate pasa, el job `generate-and-wrap-dek` ejecuta:
   - Instalación del SDK.
   - (Si `plan_only: true`) Validación de credenciales — ver abajo — y el job termina ahí.
   - (Si `plan_only: false`) Generación y wrap de la DEK, almacenamiento del secreto `dek-<domain>-<layer>-v1-wrapped` con tags de trazabilidad, y verificación de integridad automática.

### Modo `plan_only` — validación de credenciales

Ejecuta `scripts/check_auth.py`, que confirma sin efectos secundarios:

- Que las credenciales (`AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_TENANT_ID`) autentican correctamente.
- Que el Service Principal puede leer metadata de la KEK (`get_key`).
- Que el Service Principal puede listar (no leer valores) el vault de secretos.

**Limitación conocida**: esto valida permisos de *lectura*, no los de *escritura* (`wrapKey`, `setSecret`) que la ceremonia real requiere. Un `plan_only` exitoso no garantiza que el `bootstrap` real vaya a pasar el chequeo de RBAC — solo descarta errores de credenciales mal configuradas o roles completamente ausentes.

## Verificar el resultado

**Tags de trazabilidad del secreto:**

```bash
az keyvault secret show \
  --vault-name <secret-vault-name> \
  --name <secret-name> \
  --query "tags"
```

Tags esperados: `kek_vault`, `wrapped_with_kek`, `algorithm`, `provisioned_by`, `provisioned_at`, `ceremony_run_id`.

**Integridad del wrap**: se ejecuta automáticamente como parte del job (`Verify wrapped DEK integrity`). El log del step confirma longitud de 32 bytes sin exponer el valor.

## Controles de seguridad

| Control | Implementación |
|---|---|
| Generación y wrap atómico | Mismo step, mismo job — la DEK en claro nunca se serializa entre steps |
| Prohibición de persistencia en claro | Nunca se escribe a disco, `GITHUB_OUTPUT`, ni logs |
| Credencial explícita | `ClientSecretCredential` construida directamente en cada script, sin depender de `DefaultAzureCredential()` ni de sesiones de CLI |
| Scope de permisos mínimo | SP con permisos acotados a la KEK y al vault de secretos específicos, no acceso amplio |
| Aprobación de ejecución | Gate por membresía de GitHub team |
| Runner efímero | GitHub-hosted, destruido al finalizar el job |
| Trazabilidad | Tags del secreto + run ID de GitHub Actions correlacionable con logs de Key Vault |
| Verificación de integridad | Unwrap post-almacenamiento con validación de longitud, sin exponer el valor |
| Validación previa sin efectos secundarios | Modo `plan_only` para probar credenciales antes de una ejecución real |

## Notas operativas

- Trade-off de seguridad aceptado: autenticación con client secret (credencial estática) en lugar de OIDC federation, para alinear con el estándar organizacional.
- El workflow depende de `actions/checkout@v5`. GitHub está migrando el runtime de Actions de Node 20 a Node 24; si aparecen warnings de deprecación, confirmar que el job ya corre en Node 24 (mensaje "This workflow is running with Node 24 by default" en el log) — es informativo, no bloquea la ejecución.
- Este es un procedimiento de **ceremonia**, no un pipeline operativo recurrente. Se sugiere que cada ejecución quede documentada.
- Tras el aprovisionamiento inicial de todos los ambientes requeridos, evaluar deshabilitar (no eliminar) el Service Principal asociado, dado que la rotación de DEKs está proyectada a varios años.