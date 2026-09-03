# scripts/dek_bootstrap.py
#
# No habilitar logging HTTP verbose/debug del SDK de Azure en este script
# (logging_enable=True, AZURE_LOG_LEVEL=debug, etc.): el body de la llamada
# wrap_key() de más abajo contiene la DEK en claro, y un logger en modo debug
# puede volcar cuerpos de request/response al log del step.
import os
import base64
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from azure.identity import DefaultAzureCredential
from azure.keyvault.keys.crypto import CryptographyClient, KeyWrapAlgorithm
from azure.keyvault.secrets import SecretClient

kek_vault_url = os.environ["KEK_VAULT_URL"]
kek_name = os.environ["KEK_NAME"]
secret_vault_url = os.environ["SECRET_VAULT_URL"]
dek_name = os.environ["SECRET_NAME"]

credential = DefaultAzureCredential()

# 1. Resolver el binario de OpenSSL por ruta absoluta al build pinneado y
#    verificado por SHA256 (si el step de build lo generó), en vez de confiar
#    en el orden del PATH — evita que otro "openssl" presente en el PATH
#    reemplace silenciosamente al binario auditado.
pinned_openssl = Path(os.environ.get("OPENSSL_INSTALL_DIR", ""), "bin", "openssl")
openssl_bin = str(pinned_openssl) if pinned_openssl.is_file() else (shutil.which("openssl") or "openssl")

# 2. Generar DEK en memoria usando OpenSSL RAND_bytes — nunca se escribe a disco
raw_dek = subprocess.check_output([openssl_bin, "rand", "32"], text=False, timeout=10)
if len(raw_dek) != 32:
    raise RuntimeError(f"Unexpected DEK length from openssl rand: {len(raw_dek)}")

# bytearray (mutable) en vez de bytes: permite sobrescribir el buffer en
# memoria antes de descartarlo. "dek_bytes = None" por sí solo únicamente
# suelta la referencia — no borra el contenido, porque bytes es inmutable.
dek_bytes = bytearray(raw_dek)
raw_dek = None

# 3. Obtener referencia a la KEK y envolver.
#    Nota: puede aparecer en el log "Local wrap operation failed:
#    'bytearray' object is not an instance of 'bytes'". Es inofensivo — el
#    SDK intenta primero un wrap local (usa `cryptography`, que exige
#    `bytes` estricto vía isinstance) y, al fallar por el tipo bytearray,
#    reintenta automáticamente contra el servicio de Key Vault, que sí
#    acepta bytearray. Se deja así a propósito: convertir a bytes() aquí
#    para silenciar el mensaje crearía una copia inmutable adicional de la
#    DEK en claro que no se puede zerar, contradiciendo el punto 4.
kek_identifier = f"{kek_vault_url}/keys/{kek_name}"
crypto_client = CryptographyClient(kek_identifier, credential)
wrap_result = crypto_client.wrap_key(KeyWrapAlgorithm.rsa_oaep_256, dek_bytes)
wrapped_dek_b64 = base64.b64encode(wrap_result.encrypted_key).decode("utf-8")
# Versión exacta de la KEK usada (el cliente se crea sin versión — Key Vault
# resuelve a la versión vigente al momento del wrap). Se guarda como tag para
# que una futura rotación sepa con qué versión desenvolver este secreto.
kek_version_used = wrap_result.key_id.split("/")[-1]

# 4. Sobrescribir el buffer en memoria (best-effort: no hay mlock/memset a
#    nivel de página en Python puro, y no cubre copias internas que el SDK
#    de Azure haya hecho durante la llamada de red) y soltar la referencia
for i in range(len(dek_bytes)):
    dek_bytes[i] = 0
dek_bytes = None

# 5. Persistir SOLO el valor envuelto, con metadata de trazabilidad
secret_client = SecretClient(vault_url=secret_vault_url, credential=credential)
secret_name = f"{dek_name}"

secret_client.set_secret(
    secret_name,
    wrapped_dek_b64,
    tags={
        "kek_vault": kek_vault_url,
        "wrapped_with_kek": kek_name,
        "wrapped_with_kek_version": kek_version_used,
        "algorithm": "RSA-OAEP-256",
        "provisioned_by": "github-actions-dek-bootstrap",
        "provisioned_at": datetime.now(timezone.utc).isoformat(),
        "ceremony_run_id": os.environ.get("GITHUB_RUN_ID", "unknown"),
    },
)

print("DEK wrapped and stored successfully.")
print(f"Run ID: {os.environ.get('GITHUB_RUN_ID')}")
# NO imprimir wrapped_dek_b64 ni dek_bytes bajo ninguna circunstancia