# scripts/rotate_kek.py
#
# Re-envuelve (rewrap) la DEK ya almacenada en Key Vault: la desenvuelve con
# la versión VIEJA de la KEK y la vuelve a envolver con la versión VIGENTE
# (nueva) de esa misma key. No genera una DEK nueva ni rota la KEK en Key
# Vault — la rotación de la key en sí (nueva versión) debe haber ocurrido
# antes, en Key Vault (política automática o `az keyvault key rotate`
# manual). Este script solo re-protege el secreto ya existente bajo esa
# versión nueva.
#
# La DEK en claro nunca se escribe a disco, nunca se pasa por GITHUB_OUTPUT,
# y nunca se imprime en logs.
#
# No habilitar logging HTTP verbose/debug del SDK de Azure en este script:
# tanto unwrap_key() como wrap_key() transmiten la DEK en claro en el body
# de la llamada a Key Vault — aquí aplica doblemente, ya que el proceso
# sostiene el valor en claro entre ambas operaciones.

import os
import base64
import sys
from datetime import datetime, timezone
from azure.identity import ClientSecretCredential
from azure.keyvault.secrets import SecretClient
from azure.keyvault.keys.crypto import CryptographyClient, KeyWrapAlgorithm
from azure.core.exceptions import ClientAuthenticationError, HttpResponseError

kek_vault_url = os.environ["KEK_VAULT_URL"]
kek_name = os.environ["KEK_NAME"]
secret_vault_url = os.environ["SECRET_VAULT_URL"]
secret_name = os.environ["SECRET_NAME"]

EXPECTED_LENGTH_BYTES = 32  # AES-256

credential = ClientSecretCredential(
    tenant_id=os.environ["AZURE_TENANT_ID"],
    client_id=os.environ["AZURE_CLIENT_ID"],
    client_secret=os.environ["AZURE_CLIENT_SECRET"],
)

secret_client = SecretClient(vault_url=secret_vault_url, credential=credential)

# 1. Leer el secreto envuelto actual (ciphertext, seguro de manejar) y sus tags
current_secret = secret_client.get_secret(secret_name)
wrapped_dek_bytes = base64.b64decode(current_secret.value)
prior_tags = current_secret.properties.tags or {}

# 2. Resolver la versión vieja de la KEK desde la tag guardada al
#    bootstrap/última rotación. Sin esto no hay forma confiable de saber
#    qué versión desenvuelve correctamente este secreto.
old_kek_version = prior_tags.get("wrapped_with_kek_version")
if not old_kek_version:
    print(
        "ERROR: no se pudo determinar la versión de KEK usada para envolver "
        f"'{secret_name}' (no hay tag 'wrapped_with_kek_version' en el "
        "secreto — fue creado manualmente o antes de que este script "
        "empezara a guardarla)."
    )
    sys.exit(1)

# 3. Desenvolver con la versión VIEJA explícita de la KEK
try:
    old_kek_identifier = f"{kek_vault_url}/keys/{kek_name}/{old_kek_version}"
    old_crypto_client = CryptographyClient(old_kek_identifier, credential)
    unwrap_result = old_crypto_client.unwrap_key(KeyWrapAlgorithm.rsa_oaep_256, wrapped_dek_bytes)
except (ClientAuthenticationError, HttpResponseError) as e:
    print(f"ERROR al desenvolver con la versión vieja de la KEK ({old_kek_version}): {e}")
    sys.exit(1)

# bytearray mutable para poder sobrescribir con ceros antes de descartar —
# mismo patrón best-effort que dek_bootstrap.py/verify_dek.py. Límite
# honesto: esto zera nuestra copia, no la copia interna que unwrap_result.key
# (bytes, inmutable) ya traía desde el SDK.
dek_bytes = bytearray(unwrap_result.key)
unwrap_result = None

# 4. Validar longitud ANTES de tocar la versión nueva de la KEK
if len(dek_bytes) != EXPECTED_LENGTH_BYTES:
    dek_length = len(dek_bytes)
    for i in range(len(dek_bytes)):
        dek_bytes[i] = 0
    dek_bytes = None
    print(
        f"ERROR: longitud inesperada tras unwrap ({dek_length} bytes, se "
        f"esperaban {EXPECTED_LENGTH_BYTES}). Posible corrupción del wrap o "
        "mismatch de versión de KEK. Abortando antes de re-envolver."
    )
    sys.exit(1)

# 5. Envolver con la key SIN versión — igual que dek_bootstrap.py, Key Vault
#    resuelve automáticamente a la versión vigente (la nueva, post-rotación).
new_kek_identifier = f"{kek_vault_url}/keys/{kek_name}"
new_crypto_client = CryptographyClient(new_kek_identifier, credential)
wrap_result = new_crypto_client.wrap_key(KeyWrapAlgorithm.rsa_oaep_256, dek_bytes)
new_wrapped_dek_b64 = base64.b64encode(wrap_result.encrypted_key).decode("utf-8")
new_kek_version = wrap_result.key_id.split("/")[-1]

# 6. Zerar el buffer en memoria (best-effort) y soltar la referencia
for i in range(len(dek_bytes)):
    dek_bytes[i] = 0
dek_bytes = None

# 7. Guard de no-op: si la versión resuelta es la misma que la vieja, la KEK
#    todavía no fue rotada en Key Vault — no hay nada que re-proteger.
if new_kek_version == old_kek_version:
    print(
        f"ERROR: la versión vigente de la KEK ({new_kek_version}) es la misma "
        f"que la versión vieja ({old_kek_version}). La KEK no ha sido rotada "
        "en Key Vault todavía — no hay nada que hacer. Rota la key en Azure "
        "antes de correr esta ceremonia."
    )
    sys.exit(1)

# 8. Persistir SOLO el valor re-envuelto, con metadata de trazabilidad.
#    set_secret() es la única llamada mutadora de todo el script y va al
#    final: si cualquier paso anterior falla, no se escribe nada.
secret_client.set_secret(
    secret_name,
    new_wrapped_dek_b64,
    tags={
        "kek_vault": kek_vault_url,
        "wrapped_with_kek": kek_name,
        "wrapped_with_kek_version": new_kek_version,
        "algorithm": "RSA-OAEP-256",
        "rotated_from_kek_version": old_kek_version,
        "rotated_at": datetime.now(timezone.utc).isoformat(),
        "rotation_run_id": os.environ.get("GITHUB_RUN_ID", "unknown"),
        "provisioned_by": prior_tags.get("provisioned_by", "unknown"),
        "provisioned_at": prior_tags.get("provisioned_at", "unknown"),
    },
)

# 9. Lectura de verificación barata — confirma que la tag quedó como se espera
readback_tags = secret_client.get_secret(secret_name).properties.tags or {}
if readback_tags.get("wrapped_with_kek_version") != new_kek_version:
    print("ERROR: la tag 'wrapped_with_kek_version' no quedó como se esperaba tras el rewrap.")
    sys.exit(1)

print("KEK rotation complete.")
print(f"Rotado de versión {old_kek_version} a {new_kek_version}.")
print(f"Run ID: {os.environ.get('GITHUB_RUN_ID')}")
# NO imprimir new_wrapped_dek_b64 ni dek_bytes bajo ninguna circunstancia
