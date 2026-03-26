import os
from cryptography.fernet import Fernet
import base64
from dotenv import load_dotenv

# Load key from .env file if it exists
load_dotenv()

# --- MASTER KEY CONFIGURATION ---
# In production, this MUST be set in an environment variable.
# For temporary use/demo, we can load a fallback (NOT RECOMMENDED for real data).
ENCRYPTION_KEY = os.environ.get("MASTER_ENCRYPTION_KEY")

if not ENCRYPTION_KEY:
    # Generate a sample key if not present (only for first-time setup/development)
    # WARNING: Data encrypted with this key will be lost if the key changes!
    ENCRYPTION_KEY = Fernet.generate_key().decode()
    print("WARNING: 'MASTER_ENCRYPTION_KEY' env var not found. Using a temporary generated key.")
    print(f"To persist data, set 'MASTER_ENCRYPTION_KEY' to: {ENCRYPTION_KEY}")

fernet = Fernet(ENCRYPTION_KEY.encode())

def encrypt_string(data: str) -> str:
    """Encrypts a string and returns a base64 encoded token."""
    if not data:
        return ""
    if not isinstance(data, str):
        data = str(data)
    encrypted = fernet.encrypt(data.encode())
    return encrypted.decode()

def decrypt_string(encrypted_data: str) -> str:
    """Decrypts a base64 encoded token and returns the original string."""
    if not encrypted_data:
        return ""
    try:
        decrypted = fernet.decrypt(encrypted_data.encode())
        return decrypted.decode()
    except Exception as e:
        print(f"Decryption Error: {e}")
        return encrypted_data # Return raw if decryption fails (e.g., if it wasn't encrypted)

def encrypt_dict(data: dict, keys_to_encrypt: list) -> dict:
    """Recursively encrypts specific keys in a dictionary."""
    new_data = data.copy()
    for key in keys_to_encrypt:
        if key in new_data and new_data[key]:
            if isinstance(new_data[key], str):
                new_data[key] = encrypt_string(new_data[key])
            elif isinstance(new_data[key], (int, float)):
                new_data[key] = encrypt_string(str(new_data[key]))
    return new_data

def decrypt_dict(data: dict, keys_to_decrypt: list) -> dict:
    """Recursively decrypts specific keys in a dictionary."""
    new_data = data.copy()
    for key in keys_to_decrypt:
        if key in new_data and new_data[key]:
            new_data[key] = decrypt_string(new_data[key])
    return new_data
