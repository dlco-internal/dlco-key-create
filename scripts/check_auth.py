# scripts/check_auth.py
#
# Validación liviana de credenciales para el modo plan_only.
# No genera, envuelve, ni almacena ninguna DEK. Solo confirma que el
# Service Principal puede autenticarse y tiene visibilidad sobre los
# vaults configurados, para detectar errores de credenciales/permisos
# antes de disparar una ejecución real de la ceremonia.
 
import os
import sys
from azure.identity import ClientSecretCredential
from azure.keyvault.keys import KeyClient
from azure.keyvault.secrets import SecretClient
from azure.core.exceptions import ClientAuthenticationError, HttpResponseError
 
kek_vault_url = os.environ["KEK_VAULT_URL"]
secret_vault_url = os.environ["SECRET_VAULT_URL"]
kek_name = os.environ["KEK_NAME"]
 
print("=== Validación de credenciales (plan_only) ===")
print(f"KEK vault: {kek_vault_url}")
print(f"Secret vault: {secret_vault_url}")
print()
 
# 1. Construir la credencial explícita — si faltan env vars, falla aquí
try:
    credential = ClientSecretCredential(
        tenant_id=os.environ["AZURE_TENANT_ID"],
        client_id=os.environ["AZURE_CLIENT_ID"],
        client_secret=os.environ["AZURE_CLIENT_SECRET"],
    )
except KeyError as e:
    print(f"ERROR: falta la variable de entorno {e}. Verifica los secrets/vars del step.")
    sys.exit(1)
 
exit_code = 0
 
# 2. Verificar autenticación + permiso de lectura sobre la KEK
#    (list/get de metadata de la key, no requiere wrapKey)
try:
    key_client = KeyClient(vault_url=kek_vault_url, credential=credential)
    key = key_client.get_key(kek_name)
    print(f"OK — KEK '{kek_name}' accesible. Tipo: {key.key_type}, habilitada: {key.properties.enabled}")
except ClientAuthenticationError as e:
    print(f"ERROR de autenticación contra {kek_vault_url}: credenciales inválidas o expiradas.")
    print(f"  Detalle: {e}")
    exit_code = 1
except HttpResponseError as e:
    print(f"ERROR: el Service Principal no tiene permisos suficientes sobre la KEK '{kek_name}', "
          f"o la key no existe en {kek_vault_url}.")
    print(f"  Detalle: {e.message if hasattr(e, 'message') else e}")
    exit_code = 1
 
# 3. Verificar autenticación + permiso de lectura sobre el vault de secretos
#    (list de secretos, no requiere setSecret)
try:
    secret_client = SecretClient(vault_url=secret_vault_url, credential=credential)
    # list_properties_of_secrets no expone valores, solo metadata — seguro de iterar
    next(secret_client.list_properties_of_secrets(), None)
    print(f"OK — Vault de secretos '{secret_vault_url}' accesible para lectura.")
except ClientAuthenticationError as e:
    print(f"ERROR de autenticación contra {secret_vault_url}: credenciales inválidas o expiradas.")
    print(f"  Detalle: {e}")
    exit_code = 1
except HttpResponseError as e:
    print(f"ERROR: el Service Principal no tiene permisos de lectura sobre {secret_vault_url}.")
    print(f"  Detalle: {e.message if hasattr(e, 'message') else e}")
    exit_code = 1
 
print()
if exit_code == 0:
    print("Validación completa: credenciales y permisos OK. Listo para ejecutar la ceremonia real.")
else:
    print("Validación con errores — revisar antes de ejecutar con plan_only=false.")
 
sys.exit(exit_code)