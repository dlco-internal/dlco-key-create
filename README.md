# dlco-key-create

Ceremonia de aprovisionamiento inicial de **Data Encryption Keys (DEK)** simétricas para el modelo de envelope encryption de la plataforma de datos, ejecutada vía GitHub Actions con autenticación federada (OIDC) contra Azure Key Vault.

> Evento único de bootstrap por ambiente. No es un flujo recurrente ni operativo — ver [Alcance](#alcance).

## Alcance

Este repositorio contiene el workflow y los scripts que:

1. Generan una DEK simétrica (AES-256) en memoria del runner.
2. La envuelven (wrap) usando la KEK del segmento correspondiente, vía Azure Key Vault.
3. Persisten únicamente el valor envuelto (ciphertext) como secreto en Key Vault, con metadata de trazabilidad.
4. Verifican la integridad del wrap desenvolviendo el secreto y validando su longitud, sin exponer el valor en claro.

La DEK en texto plano **nunca** persiste fuera de la memoria del runner, nunca se escribe a disco, nunca se pasa por `GITHUB_OUTPUT`, y nunca se imprime en logs.

## Estructura del repositorio

```
.
├── .github/
│   └── workflows/
│       └── dek-bootstrap.yml      # Workflow de la ceremonia
├── scripts/
│   ├── dek_bootstrap.py           # Generación + wrap + almacenamiento
│   └── verify_dek.py              # Verificación post-wrap (unwrap + validación de longitud)
└── README.md
```

## Prerrequisitos

- KEK del ambiente ya provisionada en Key Vault.
- App Registration en Entra ID con **federated credential** configurado (sin client secret) — ver [Configuración de identidad](#configuración-de-identidad-oidc-federation).
- Rol `Key Vault Crypto User` asignado al Service Principal sobre la KEK (permite `wrapKey`/`unwrapKey`).
- Rol `Key Vault Secrets Officer` asignado al Service Principal sobre el vault de almacenamiento de secretos (permite `setSecret`/`getSecret`).
- GitHub Environment `dek-bootstrap` configurado (ver [Configuración del Environment](#configuración-del-environment-en-github)).
- Tres GitHub Secrets a nivel de repositorio: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`.

## Configuración de identidad (OIDC Federation)

Autenticación sin secretos estáticos, vía Workload Identity Federation.

```bash
# 1. Crear App Registration
az ad app create --display-name "github-actions-dek-bootstrap"

# 2. Crear Service Principal asociado
az ad sp create --id <APP_ID>

# 3. Crear federated credential (el subject debe coincidir EXACTAMENTE
#    con org/repo y el nombre del GitHub Environment)
az ad app federated-credential create \
  --id <APP_ID> \
  --parameters '{
    "name": "github-dek-bootstrap",
    "issuer": "https://token.actions.githubusercontent.com",
    "subject": "repo:<org>/<repo>:environment:dek-bootstrap",
    "audiences": ["api://AzureADTokenExchange"]
  }'

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

> El orden importa: crea primero el GitHub Environment (paso siguiente) y luego el federated credential, para evitar tener que corregir el `subject` si el nombre del Environment cambia.

### Opción manual — Azure Portal

Alternativa a los comandos CLI anteriores, útil si no tienes acceso a Azure CLI o si otro equipo (ej. Seguridad/IAM) debe ejecutar estos pasos por segregación de funciones.

**1. Crear el App Registration**

- Entra ID → **App registrations** → **New registration**.
- Nombre: `github-actions-dek-bootstrap`.
- Supported account types: *Accounts in this organizational directory only* (single tenant).
- **Register**.
- En la página **Overview** de la app recién creada, copiar el **Application (client) ID** → este es tu `AZURE_CLIENT_ID`.

**2. Crear el federated credential**

- En la misma app → **Certificates & secrets** → pestaña **Federated credentials** → **Add credential**.
- Scenario: *GitHub Actions deploying Azure resources*.
- Organization: tu organización de GitHub.
- Repository: el nombre del repositorio.
- Entity type: **Environment**.
- GitHub environment name: `dek-bootstrap` (debe coincidir carácter por carácter con el Environment creado en GitHub).
- Name: `github-dek-bootstrap`.
- **Add**.

**3. Asignar el rol sobre la KEK**

- Ir al Key Vault que contiene la KEK → **Access control (IAM)** → **Add** → **Add role assignment**.
- Role: `Key Vault Crypto User`.
- Members: buscar y seleccionar `github-actions-dek-bootstrap` (el App Registration).
- **Review + assign**.
- Nota: el Portal generalmente asigna el rol a nivel de vault completo. Para scope granular sobre una key específica, usar el blade de la key individual (**Keys** → seleccionar la KEK → **Access control (IAM)**) si el SKU lo permite, o usar CLI.

**4. Asignar el rol sobre el vault de secretos**

- Repetir el paso anterior sobre el Key Vault donde se almacenará la DEK envuelta (puede ser el mismo vault u otro distinto).
- Role: `Key Vault Secrets Officer`.

**5. Obtener Tenant ID y Subscription ID**

- **Tenant ID**: Entra ID → **Overview** → campo *Tenant ID*.
- **Subscription ID**: **Subscriptions** → seleccionar la suscripción correspondiente → campo *Subscription ID* en Overview.

**6. Cargar los valores en GitHub**

- Settings → **Secrets and variables** → **Actions** → **New repository secret**, uno por cada valor: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`.

## Configuración del Environment en GitHub

Settings → Environments → New environment → `dek-bootstrap`.

- **Deployment branches and tags**: restringir a la rama autorizada (no "No restriction").
- **Required reviewers**: mínimo 1, idealmente 2 para segregación de funciones.
- **Wait timer**: opcional, mismas restricciones de plan que Required reviewers.

## Ejecución

1. Ve a la pestaña **Actions** → `DEK Bootstrap Ceremony` → **Run workflow**.
2. Ingresa `kek_vault_url`, 'kek_name', `secret_vault_url` y 'secret_name', según corresponda.
3. Si el Environment tiene reviewers configurados, el run queda en espera de aprobación.
4. Tras la aprobación, el job ejecuta:
   - Autenticación OIDC contra Azure.
   - Generación y wrap de la DEK.
   - Almacenamiento del secreto con tags de trazabilidad.
   - Verificación de integridad (unwrap + validación de longitud, sin exponer el valor).

## Verificar el resultado

**Tags de trazabilidad del secreto:**

```bash
az keyvault secret show \
  --vault-name <secret-vault-name> \
  --name <secret-name> \
  --query "tags"
```

Tags esperados: `kek_vault_url`, `wrapped_with_kek`, `algorithm`, `provisioned_by`, `provisioned_at`, `ceremony_run_id`.

**Integridad del wrap**: se ejecuta automáticamente como parte del job (`Verify wrapped DEK integrity`). El log del step confirma longitud de 32 bytes sin exponer el valor.

## Controles de seguridad

| Control | Implementación |
|---|---|
| Sin secretos estáticos | Autenticación 100% OIDC federation |
| Generación y wrap atómico | Mismo step, mismo job — la DEK en claro nunca se serializa entre steps |
| Prohibición de persistencia en claro | Nunca se escribe a disco, `GITHUB_OUTPUT`, ni logs |
| Scope de permisos mínimo | SP con permisos acotados a la KEK y al vault de secretos específicos, no acceso amplio |
| Aprobación humana | GitHub Environment con required reviewers |
| Runner efímero | GitHub-hosted, destruido al finalizar el job |
| Trazabilidad | Tags del secreto + run ID de GitHub Actions correlacionable con logs de Key Vault |
| Verificación de integridad | Unwrap post-almacenamiento con validación de longitud, sin exponer el valor |

## Notas operativas

- Tras el aprovisionamiento inicial de todos los segmentos requeridos, evaluar deshabilitar (no eliminar) el Service Principal asociado, dado que la rotación de DEKs está proyectada a varios años.
