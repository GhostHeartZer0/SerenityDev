#!/usr/bin/env python3
"""
encrypt_key.py
Utility script to encrypt a raw API key into a hardware-bound pqc_v1:... blob for .env
"""
import sys
import os

# Ensure serenitydevserver directory is in path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from serenitydevserver import SerenityKeyVault

def main():
    if len(sys.argv) < 2:
        print("Usage: python encrypt_key.py <raw_api_key>")
        sys.exit(1)
    
    raw_key = sys.argv[1].strip()
    if not raw_key:
        print("Error: Empty key provided.")
        sys.exit(1)

    encrypted_blob = SerenityKeyVault.encrypt(raw_key)
    print("\n" + "="*50)
    print("SUCCESS: Hardware-bound PQC Key Blob Generated")
    print("="*50)
    print(f"\n{encrypted_blob}\n")
    print("Set this in your .env file:")
    print(f"LOCAL_API_KEY={encrypted_blob}\n")

if __name__ == "__main__":
    main()
