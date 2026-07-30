#!/usr/bin/env python3
"""
setup.py
Installer and package configuration script for SerenityDev.

Usage:
    python setup.py                # Run full setup (pip install -r requirements.txt, .env setup, npm install)
    python setup.py install        # Standard setuptools installation
    pip install -e .               # Development mode installation
"""

import sys
import os
import subprocess
from setuptools import setup

def read_requirements():
    """Reads dependencies from requirements.txt."""
    reqs_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requirements.txt")
    if os.path.exists(reqs_path):
        with open(reqs_path, "r", encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip() and not line.startswith("#")]
    return [
        "fastapi>=0.100.0",
        "uvicorn[standard]>=0.20.0",
        "pydantic>=2.0.0",
        "httpx>=0.24.0",
        "cryptography>=41.0.0",
        "python-dotenv>=1.0.0",
        "starlette>=0.27.0",
    ]

def setup_environment():
    """Installer runner to install Python requirements, verify .env, and build frontend assets."""
    print("=" * 60)
    print("           SerenityDev Setup & Dependency Installer           ")
    print("=" * 60)

    # 1. Check Python version
    py_ver = sys.version_info
    print(f"[+] Python Version: {py_ver.major}.{py_ver.minor}.{py_ver.micro}")
    if py_ver < (3, 9):
        print("[!] Warning: SerenityDev is recommended for Python 3.9+.")

    # 2. Upgrade pip and install requirements
    print("\n[1/3] Installing Python dependencies via pip...")
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "pip"], check=True)
        req_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requirements.txt")
        subprocess.run([sys.executable, "-m", "pip", "install", "-r", req_file], check=True)
        print("[✓] Python dependencies installed successfully.")
    except subprocess.CalledProcessError as e:
        print(f"[!] Error installing dependencies: {e}")
        return False

    # 3. Check / Create .env template
    print("\n[2/3] Checking environment configuration (.env)...")
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_file):
        print("[!] .env file not found. Creating template .env file...")
        try:
            with open(env_file, "w", encoding="utf-8") as f:
                f.write("# SerenityDev Environment Configuration\n")
                f.write("# Replace LOCAL_API_KEY with your hardware-bound key using seal_secrets.py\n")
                f.write("# Example: python seal_secrets.py \"your_raw_api_key\"\n")
                f.write("# LOCAL_API_KEY=pqc_v1:...\n")
            print("[✓] Created template .env file.")
        except Exception as e:
            print(f"[!] Could not create .env file: {e}")
    else:
        print("[✓] .env file already exists.")

    # 4. Check Node.js / npm dependencies
    print("\n[3/3] Checking Node.js / Extension dependencies...")
    pkg_json = os.path.join(os.path.dirname(os.path.abspath(__file__)), "package.json")
    if os.path.exists(pkg_json):
        try:
            npm_check = subprocess.run(["npm", "--version"], capture_output=True, text=True, shell=True)
            if npm_check.returncode == 0:
                print(f"[+] Found npm (v{npm_check.stdout.strip()})")
                print("[*] Running 'npm install' for VS Code extension dependencies...")
                subprocess.run(["npm", "install"], check=True, shell=True)
                print("[✓] Extension dependencies installed successfully.")
            else:
                print("[!] npm not found in system PATH. Skipping extension npm dependencies.")
        except Exception as e:
            print(f"[!] Warning checking npm: {e}")

    print("\n" + "=" * 60)
    print(" Setup Complete! You can now start the server using:")
    print("   python serenitydevserver.py")
    print("   OR")
    print("   python start_native_mcp.py")
    print("=" * 60)
    return True

# Standard setuptools configuration
setup_args = dict(
    name="SerenityDev",
    version="1.5.0",
    description="Local Python Agent interface running on llama.cpp with llama-server backend",
    author="GhostHeartZer0",
    py_modules=[
        "serenitydevserver",
        "startup",
        "start_native_mcp",
        "config_guard",
        "encrypt_key",
        "seal_secrets",
        "build_extension",
    ],
    install_requires=read_requirements(),
    python_requires=">=3.9",
    extras_require={
        "dev": ["pytest>=7.0.0", "pytest-asyncio>=0.21.0"],
    },
)

if __name__ == "__main__":
    setuptools_commands = {"install", "develop", "sdist", "bdist", "bdist_wheel", "egg_info", "--help", "-h", "build"}
    if len(sys.argv) > 1 and any(cmd in sys.argv for cmd in setuptools_commands):
        setup(**setup_args)
    else:
        setup_environment()
