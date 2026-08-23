#!/usr/bin/env python3
"""
seal_secrets.py
SerenityDev Secret Sealer Utility
Encrypts a raw secret into a hardware-bound pqc_v1:... hex blob for .env
"""
import uuid
import hashlib
import os
import sys
import subprocess
import time

def get_machine_entropy() -> bytes:
    """Reads composite multi-factor hardware attributes (OS MAC, Windows MachineGuid, BIOS UUID)."""
    components = [str(uuid.getnode()).encode("utf-8")]
    # 1. Windows Registry MachineGuid
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as k:
            guid, _ = winreg.QueryValueEx(k, "MachineGuid")
            if guid:
                components.append(str(guid).encode("utf-8"))
    except Exception:
        pass
    # 2. BIOS / Motherboard UUID
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    uuid_found = False
    try:
        res = subprocess.run(["wmic", "csproduct", "get", "UUID"], shell=False, capture_output=True, text=True, timeout=2, creationflags=flags)
        if res.returncode == 0 and res.stdout:
            lines = [l.strip() for l in res.stdout.strip().splitlines() if l.strip() and "UUID" not in l]
            if lines:
                components.append(lines[0].encode("utf-8"))
                uuid_found = True
    except Exception:
        pass

    # Windows 11+ fallback when wmic is deprecated/absent
    if not uuid_found and sys.platform == "win32":
        try:
            ps_res = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", "(Get-CimInstance -ClassName Win32_ComputerSystemProduct).UUID"],
                shell=False, capture_output=True, text=True, timeout=3, creationflags=flags
            )
            if ps_res.returncode == 0 and ps_res.stdout.strip():
                components.append(ps_res.stdout.strip().encode("utf-8"))
        except Exception:
            pass

    combined = b"|".join(components)
    return hashlib.sha3_512(combined).digest()

def seal_secret(raw_text: str) -> str:
    """Encrypts raw secret into pqc_v1 format with hardened entropy and nonces."""
    entropy = get_machine_entropy()
    seed = os.urandom(16) + time.monotonic_ns().to_bytes(8, "big") + entropy
    nonce = hashlib.shake_256(seed).digest(12)
    plain_bytes = raw_text.encode('utf-8')

    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        derived_key = hashlib.shake_256(entropy).digest(32)
        aesgcm = AESGCM(derived_key)
        ciphertext = aesgcm.encrypt(nonce, plain_bytes, None)
    except ImportError:
        keystream = hashlib.shake_256(entropy + nonce).digest(len(plain_bytes))
        ciphertext = bytes(b ^ k for b, k in zip(plain_bytes, keystream))

    payload = nonce + ciphertext
    return f"pqc_v1:{payload.hex()}"

def update_env_file(sealed_key: str):
    """Updates or creates LOCAL_API_KEY in .env file."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    lines = []
    found = False
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith("LOCAL_API_KEY=") or line.strip().startswith("LOCALAPI_KEY=") or line.strip().startswith("LOCALAPIKEY="):
                    lines.append(f"LOCAL_API_KEY={sealed_key}\n")
                    found = True
                else:
                    lines.append(line)
    if not found:
        lines.insert(0, f"LOCAL_API_KEY={sealed_key}\n")

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    print(f"[+] Successfully written to {env_path}")

def main():
    print("--- SerenityDev Secret Sealer ---")
    auto_write = "--write-env" in sys.argv or "-w" in sys.argv
    args = [a for a in sys.argv[1:] if a not in ("--write-env", "-w")]

    if len(args) > 0:
        secret_to_hide = args[0].strip()
    else:
        secret_to_hide = input("Enter the secret string (API Key, etc.): ").strip()

    if not secret_to_hide:
        print("Error: Empty secret provided.")
        sys.exit(1)

    sealed = seal_secret(secret_to_hide)
    print("\nCopy this into your .env file:")
    print("-" * 40)
    print(f"LOCAL_API_KEY={sealed}")
    print("-" * 40 + "\n")

    if auto_write or input("Update .env file automatically? [Y/n]: ").strip().lower() in ("y", "yes", ""):
        update_env_file(sealed)

if __name__ == "__main__":
    main()

