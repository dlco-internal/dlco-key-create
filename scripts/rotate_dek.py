# scripts/rotate_dek.py
#
# Rota el VALOR de la DEK: genera una clave simétrica nueva (AES-256) vía
# OpenSSL — mismo mecanismo pinneado que dek_bootstrap.py — y la envuelve
# con la versión vigente de la KEK, almacenando el resultado como una nueva
# versión del secreto ya existente.
#
# A diferencia de rotate_kek.py, este script NUNCA desenvuelve el valor
# anterior de la DEK: no hace falta leerlo en claro porque no se reutiliza,
# solo se reemplaza. Por eso tampoco necesita saber la versión vieja de la
# KEK — todo lo que hace es generar + envolver + guardar, igual que
# dek_bootstrap.py, con el agregado de un guard que exige que el secreto ya
# haya sido bootstrapeado antes (paso 2 más abajo).
#
# Importante — alcance: esta ceremonia NO re-cifra datos fuera de Key Vault
# que ya hayan sido protegidos con la DEK anterior. Ver la nota de alcance
# en el README ("Rotación de DEK"). Esa migración es responsabilidad de
# quien consume la DEK para cifrar datos, fuera del alcance de este repo.
#
# La DEK en texto plano nunca se escribe a disco, nunca se pasa por
# GITHUB_OUTPUT, y nunca se imprime en logs.
#
# No habilitar logging HTTP verbose/debug del SDK de Azure en este script:
# el body de la llamada wrap_key() contiene la DEK en claro.

import os
import base64
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from azure.identity import ClientSecretCredential
from azure.keyvault.secrets import SecretClient
from azure.keyvault.keys.crypto import CryptographyClient, KeyWrapAlgorithm
from azure.core.exceptions import ResourceNotFoundError

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

# 1. Leer el secreto actual: sus tags (para el guard del paso 2 y para
#    preservar provisioned_by/provisioned_at) y su número de versión (para
#    el log/tag de auditoría del paso 8). Su VALOR (ciphertext) no se usa
#    para nada — nunca se desenvuelve.
try:
    current_secret = secret_client.get_secret(secret_name)
except ResourceNotFoundError:
    print(
        f"ERROR: el secreto '{secret_name}' no existe en {secret_vault_url}. "
        "Esta ceremonia rota el valor de una DEK ya existente — correr "
        "primero 'DEK Bootstrap Ceremony' antes de intentar rotarla."
    )
    sys.exit(1)

prior_tags = current_secret.properties.tags or {}
old_secret_version = current_secret.properties.version

# 2. Guard: exigir que el secreto ya haya sido bootstrapeado. La tag
#    'wrapped_with_kek_version' solo la escriben dek_bootstrap.py y
#    rotate_kek.py — si falta, este secreto no pasó por esa ceremonia
#    (creado a mano, o por otro proceso) y no es seguro asumir que "rotar"
#    tiene sentido aquí.
if not prior_tags.get("wrapped_with_kek_version"):
    print(
        f"ERROR: el secreto '{secret_name}' no tiene la tag "
        "'wrapped_with_kek_version' — no fue provisionado por "
        "'DEK Bootstrap Ceremony'. Esta ceremonia rota el VALOR de una DEK "
        "ya existente, no crea una desde cero (para eso está el bootstrap)."
    )
    sys.exit(1)

# 3. Resolver el binario de OpenSSL pinneado — mismo mecanismo exacto que
#    dek_bootstrap.py (ruta absoluta al build pinneado y verificado por
#    SHA256, en vez de confiar en el orden del PATH).
pinned_openssl = Path(os.environ.get("OPENSSL_INSTALL_DIR", ""), "bin", "openssl")
openssl_bin = str(pinned_openssl) if pinned_openssl.is_file() else (shutil.which("openssl") or "openssl")

# 4. Generar una DEK NUEVA en memoria — nunca se escribe a disco.
raw_dek = subprocess.check_output([openssl_bin, "rand", "32"], text=False, timeout=10)
if len(raw_dek) != EXPECTED_LENGTH_BYTES:
    raise RuntimeError(f"Unexpected DEK length from openssl rand: {len(raw_dek)}")

# bytearray mutable — mismo patrón que dek_bootstrap.py, para poder
# sobrescribir con ceros antes de descartar.
dek_bytes = bytearray(raw_dek)
raw_dek = None

# 5. Envolver con la KEK SIN versión — Key Vault resuelve a la versión
#    vigente. Desde este script, "envolver con la KEK vigente" y "envolver
#    con una versión nueva de la KEK" son la misma operación: lo que sea
#    que Key Vault considere vigente al momento de esta llamada.
kek_identifier = f"{kek_vault_url}/keys/{kek_name}"
crypto_client = CryptographyClient(kek_identifier, credential)
wrap_result = crypto_client.wrap_key(KeyWrapAlgorithm.rsa_oaep_256, dek_bytes)
wrapped_dek_b64 = base64.b64encode(wrap_result.encrypted_key).decode("utf-8")
kek_version_used = wrap_result.key_id.split("/")[-1]

# 6. Zerar el buffer en memoria (best-effort: no hay mlock/memset a nivel
#    de página en Python puro, y no cubre copias internas que el SDK de
#    Azure haya hecho durante la llamada de red) y soltar la referencia.
for i in range(len(dek_bytes)):
    dek_bytes[i] = 0
dek_bytes = None

# 7. Persistir SOLO el valor envuelto nuevo, con metadata de trazabilidad.
#    set_secret() es la única llamada mutadora de todo el script y va al
#    final: si cualquier paso anterior falla, no se escribe nada.
#
#    Se preservan provisioned_by/provisioned_at del bootstrap original
#    (igual que rotate_kek.py). dek_rotated_from_secret_version queda como
#    TAG (no solo como línea de log) porque las tags persisten junto al
#    secreto indefinidamente, mientras que la retención de logs de GitHub
#    Actions es limitada (90 días por defecto) — quien coordine el
#    re-cifrado externo semanas o meses después puede necesitar este dato
#    vía `az keyvault secret show` sin depender de que el log del run
#    todavía exista.
secret_client.set_secret(
    secret_name,
    wrapped_dek_b64,
    tags={
        "kek_vault": kek_vault_url,
        "wrapped_with_kek": kek_name,
        "wrapped_with_kek_version": kek_version_used,
        "algorithm": "RSA-OAEP-256",
        "provisioned_by": prior_tags.get("provisioned_by", "unknown"),
        "provisioned_at": prior_tags.get("provisioned_at", "unknown"),
        "dek_rotated_at": datetime.now(timezone.utc).isoformat(),
        "dek_rotation_run_id": os.environ.get("GITHUB_RUN_ID", "unknown"),
        "dek_rotated_from_secret_version": old_secret_version,
    },
)

# 8. Lectura de verificación barata — confirma que la tag quedó como se
#    espera. Mismo patrón que el paso 9 de rotate_kek.py.
readback_tags = secret_client.get_secret(secret_name).properties.tags or {}
if readback_tags.get("wrapped_with_kek_version") != kek_version_used:
    print("ERROR: la tag 'wrapped_with_kek_version' no quedó como se esperaba tras la rotación de DEK.")
    sys.exit(1)

print("DEK rotation complete (nuevo valor generado, envuelto y almacenado).")
print(f"Secreto anterior (superseded, aún recuperable en Key Vault): version {old_secret_version}")
print(f"Nueva versión del secreto envuelta con KEK version: {kek_version_used}")
print(f"Run ID: {os.environ.get('GITHUB_RUN_ID')}")
# NO imprimir wrapped_dek_b64 ni dek_bytes bajo ninguna circunstancia
