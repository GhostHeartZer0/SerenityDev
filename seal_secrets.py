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

def generate_machine_entropy() -> bytes:
    """Generates hardware-bound seed using MAC address string and SHA3-512."""
    mac = str(uuid.getnode()).encode('utf-8')
    return hashlib.sha3_512(mac).digest()

def seal_secret(raw_text: str) -> str:
    """Encrypts raw secret into pqc_v1 format with 12-byte random nonce."""
    entropy = generate_machine_entropy()
    nonce = os.urandom(12)
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

def main():
    print("--- SerenityDev Secret Sealer ---")
    if len(sys.argv) > 1:
        secret_to_hide = sys.argv[1].strip()
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

if __name__ == "__main__":
    main()
