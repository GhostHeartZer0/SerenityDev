import os
import sys
import site
import platform

def activate_virtualenv():
    """Detects and activates local .venv or venv environment into sys.path and os.environ."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = []
    
    # Check explicit VIRTUAL_ENV if set
    if os.environ.get("VIRTUAL_ENV"):
        candidates.append(os.environ["VIRTUAL_ENV"])
    
    # Check if already running in virtual environment (sys.prefix != sys.base_prefix)
    if getattr(sys, "base_prefix", sys.prefix) != sys.prefix:
        candidates.append(sys.prefix)

    candidates.extend([
        os.path.join(script_dir, ".venv"),
        os.path.join(script_dir, "venv"),
        os.path.join(os.getcwd(), ".venv"),
        os.path.join(os.getcwd(), "venv"),
        os.path.join(os.path.expanduser("~"), "SerenityDev", ".venv"),
        os.path.join(os.path.expanduser("~"), ".venv")
    ])
    
    venv_dir = next((os.path.abspath(c) for c in candidates if c and os.path.isdir(c)), None)
    if not venv_dir:
        return None

    platform_name = platform.system().lower()
    if platform_name == "windows":
        bin_dir = os.path.join(venv_dir, "Scripts")
        site_packages = os.path.join(venv_dir, "Lib", "site-packages")
    else:
        bin_dir = os.path.join(venv_dir, "bin")
        lib_dir = os.path.join(venv_dir, "lib")
        site_packages = os.path.join(venv_dir, "lib", "python3", "site-packages")
        if os.path.isdir(lib_dir):
            candidate_dirs = [
                os.path.join(lib_dir, d, "site-packages")
                for d in os.listdir(lib_dir)
                if d.startswith("python") and os.path.isdir(os.path.join(lib_dir, d))
            ]
            for candidate in candidate_dirs:
                if os.path.isdir(candidate):
                    site_packages = candidate
                    break

    os.environ["VIRTUAL_ENV"] = venv_dir
    if os.path.exists(bin_dir):
        path_env = os.environ.get("PATH", "")
        if bin_dir not in path_env:
            os.environ["PATH"] = bin_dir + os.pathsep + path_env

    if os.path.exists(site_packages) and site_packages not in sys.path:
        site.addsitedir(site_packages)
        sys.path.insert(0, site_packages)

    return venv_dir

def _load_env_file(env_path: str):
    """Fallback .env parser using Python standard library."""
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key, val = key.strip(), val.strip().strip("'\"")
                if key and key not in os.environ:
                    os.environ[key] = val

def initialize_environment():
    """Strictly loads and validates environment variables and virtualenv."""
    activated_venv = activate_virtualenv()
    if activated_venv:
        print(f"[+] Virtual Environment Activated: {activated_venv}")

    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=env_path)
    except ImportError:
        _load_env_file(env_path)
    
    api_key = os.getenv("LOCAL_API_KEY") or os.getenv("LOCALAPI_KEY") or os.getenv("LOCALAPIKEY")
    
    if not api_key:
        print("[*] LOCAL_API_KEY missing from environment. Generating hardware-bound PQC key...")
        try:
            from seal_secrets import seal_secret, update_env_file
            sealed = seal_secret("serenity_local_session_master_key")
            update_env_file(sealed)
            os.environ["LOCAL_API_KEY"] = sealed
            api_key = sealed
            print(f"[+] Successfully generated and sealed hardware key into {env_path}")
        except Exception as e:
            print(f"[!] Warning during auto-sealing key: {e}")

    if not api_key:
        raise RuntimeError(
            "CRITICAL ERROR: [Auth] LOCALAPIKEY is missing from environment.\n"
            "This prevents the PQC-Vault from initializing.\n"
            "Shutting down to prevent insecure operation."
        )

    if not api_key.startswith("pqc_v1:"):
        print("[*] Encrypting raw key to pqc_v1 format...")
        try:
            from seal_secrets import seal_secret, update_env_file
            sealed = seal_secret(api_key)
            update_env_file(sealed)
            os.environ["LOCAL_API_KEY"] = sealed
            api_key = sealed
        except Exception as e:
            raise RuntimeError(
                f"CRITICAL ERROR: [Auth] Failed to auto-encrypt key: {e}\n"
                "The server will not run on unencrypted keys."
            )
    print("[+] Environment Validated: Hardware-bound identity confirmed.")

