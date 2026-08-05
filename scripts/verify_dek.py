# scripts/verify_dek.py
#
# Verifica que la DEK envuelta (wrapped) almacenada en Key Vault puede
# desenvolverse correctamente con la KEK correspondiente, y que el
# resultado tiene la longitud esperada (32 bytes = AES-256).
#
# El valor desenvuelto NUNCA se imprime, se loguea, ni se persiste.
# Solo se valida su longitud y luego se descarta.
#
# No habilitar logging HTTP verbose/debug del SDK de Azure en este script:
# el body de la respuesta de unwrap_key() contiene la DEK en claro.

import os
import base64
import sys
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient
from azure.keyvault.keys.crypto import CryptographyClient, KeyWrapAlgorithm
 
kek_vault_url = os.environ["KEK_VAULT_URL"]
kek_name = os.environ["KEK_NAME"]
secret_vault_url = os.environ["SECRET_VAULT_URL"]
secret_name = os.environ["SECRET_NAME"]
 
EXPECTED_LENGTH_BYTES = 32  # AES-256
 
credential = DefaultAzureCredential()
 
# 1. Leer el secreto envuelto (ciphertext, seguro de manejar/loguear)
secret_client = SecretClient(vault_url=secret_vault_url, credential=credential)
wrapped_dek_b64 = secret_client.get_secret(secret_name).value
wrapped_dek_bytes = base64.b64decode(wrapped_dek_b64)
 
# 2. Desenvolver con la KEK
kek_identifier = f"{kek_vault_url}/keys/{kek_name}"
crypto_client = CryptographyClient(kek_identifier, credential)
result = crypto_client.unwrap_key(KeyWrapAlgorithm.rsa_oaep_256, wrapped_dek_bytes)
 
# 3. Copiar a bytearray mutable y sobrescribir con ceros antes de descartar —
#    mismo patrón best-effort que dek_bootstrap.py. Límite honesto: esto zera
#    nuestra copia, no la copia interna que result.key (bytes, inmutable) ya
#    traía desde el SDK — no hay forma segura de mutar esa in-place desde
#    Python puro. Validar longitud SIN imprimir el valor en claro.
dek_bytes = bytearray(result.key)
result = None  # descartar la referencia del SDK cuanto antes
dek_length = len(dek_bytes)
for i in range(len(dek_bytes)):
    dek_bytes[i] = 0
dek_bytes = None

if dek_length != EXPECTED_LENGTH_BYTES:
    print(
        f"ERROR: longitud inesperada tras unwrap ({dek_length} bytes, "
        f"se esperaban {EXPECTED_LENGTH_BYTES}). Posible corrupción del wrap "
        f"o mismatch entre KEK usada para wrap y unwrap."
    )
    sys.exit(1)
 
print("Verificación OK del secreto configurado.")
print(f"Longitud de la DEK desenvuelta: {dek_length} bytes (AES-256)")
print(f"KEK utilizada para validación: {kek_name}")
print("El valor en claro no fue impreso ni persistido.")
 