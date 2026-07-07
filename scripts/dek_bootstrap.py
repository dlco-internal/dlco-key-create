# scripts/dek_bootstrap.py
import os
import base64
import json
from datetime import datetime, timezone
from azure.identity import DefaultAzureCredential
from azure.keyvault.keys.crypto import CryptographyClient, KeyWrapAlgorithm
from azure.keyvault.secrets import SecretClient

domain = os.environ["DOMAIN"]
kek_vault_url = os.environ["KEK_VAULT_URL"]
kek_name = os.environ["KEK_NAME"]
secret_vault_url = os.environ["SECRET_VAULT_URL"]

credential = DefaultAzureCredential()

# 1. Generar DEK en memoria — nunca se escribe a disco
dek_bytes = os.urandom(32)  # AES-256

# 2. Obtener referencia a la KEK y envolver
kek_identifier = f"{kek_vault_url}/keys/{kek_name}"
crypto_client = CryptographyClient(kek_identifier, credential)
wrap_result = crypto_client.wrap_key(KeyWrapAlgorithm.rsa_oaep_256, dek_bytes)
wrapped_dek_b64 = base64.b64encode(wrap_result.encrypted_key).decode("utf-8")

# 3. Borrar la referencia en claro explícitamente (best-effort en Python)
dek_bytes = None

# 4. Persistir SOLO el valor envuelto, con metadata de trazabilidad
secret_client = SecretClient(vault_url=secret_vault_url, credential=credential)
secret_name = f"dek-{domain}-wrapped"

secret_client.set_secret(
    secret_name,
    wrapped_dek_b64,
    tags={
        "domain": domain,
        "wrapped_with_kek": kek_name,
        "algorithm": "RSA-OAEP-256",
        "provisioned_by": "github-actions-dek-bootstrap",
        "provisioned_at": datetime.now(timezone.utc).isoformat(),
        "ceremony_run_id": os.environ.get("GITHUB_RUN_ID", "unknown"),
    },
)

print(f"DEK wrapped and stored as secret: {secret_name}")
print(f"Run ID: {os.environ.get('GITHUB_RUN_ID')}")
# NO imprimir wrapped_dek_b64 ni dek_bytes bajo ninguna circunstancia
