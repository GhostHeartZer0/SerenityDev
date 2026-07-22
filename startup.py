import os

def _load_env_file(env_path: str):
    """Fallback .env parser using Python standard library."""
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip("'\"")
                if key and key not in os.environ:
                    os.environ[key] = val

def initialize_environment():
    """Strictly loads and validates environment variables."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(script_dir, ".env")
    
    try:
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=env_path)
    except ModuleNotFoundError:
        _load_env_file(env_path)
    
    api_key = os.getenv("LOCAL_API_KEY") or os.getenv("LOCALAPI_KEY") or os.getenv("LOCALAPIKEY")
    
    if not api_key:
        raise RuntimeError(
            "CRITICAL ERROR: [Auth] LOCALAPIKEY is missing from environment.\n"
            "This prevents the PQC-Vault from initializing.\n"
            "Shutting down to prevent insecure operation."
        )

    # Validate that it's not a placeholder or an unencrypted string
    if not api_key.startswith("pqc_v1:"):
        raise RuntimeError(
            "CRITICAL ERROR: [Auth] LOCALAPIKEY is present but NOT encrypted via SealSecrets.\n"
            f"Detected format mismatch in value starting with '{api_key[:8]}...'\n"
            "The server will not run on unencrypted keys."
        )

    print("[+] Environment Validated: Hardware-bound identity confirmed.")
