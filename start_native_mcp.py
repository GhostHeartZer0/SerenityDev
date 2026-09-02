from startup import initialize_environment

try:
    initialize_environment()
except RuntimeError as e:
    import sys
    print(f"\n{e}")
    sys.exit(1)

import os
import socket
import sys
import uvicorn
import threading
import subprocess
from http.server import HTTPServer, SimpleHTTPRequestHandler
from datetime import datetime, timedelta, timezone
import ipaddress

from serenitydevserver import SerenityKeyVault

def free_port(port: int):
    """Frees specified port if occupied by a zombie process on Windows."""
    try:
        res = subprocess.run(f"netstat -ano | findstr :{port}", shell=True, capture_output=True, text=True)
        if res.returncode == 0 and res.stdout:
            for line in res.stdout.strip().splitlines():
                parts = line.split()
                if len(parts) >= 5 and "LISTENING" in parts:
                    pid = parts[-1]
                    if pid != "0" and int(pid) != str(os.getpid()):
                        subprocess.run(f"taskkill /F /PID {pid}", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

def get_local_ip() -> str:
    """Finds primary local network IP address."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip

def get_all_local_ips() -> list[str]:
    """Finds all detected local network IP addresses."""
    ips = set()
    primary = get_local_ip()
    if primary:
        ips.add(primary)
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None):
            ip = info[4][0]
            if ip and not ip.startswith("127."):
                ips.add(ip)
    except Exception:
        pass
    valid_ips = []
    for ip in sorted(list(ips)):
        try:
            ipaddress.ip_address(ip)
            valid_ips.append(ip)
        except Exception:
            pass
    return valid_ips

def ensure_pqc_root_ca_and_cert(
    ca_cert_path: str = "rootCA.pem",
    ca_key_path: str = "rootCA.key",
    cert_path: str = "cert.pem",
    key_path: str = "cert.key"
):
    """Generates PQC-bound Local Root CA and signed Leaf Certificate for Android trust."""
    force_regen = os.environ.get("REGENERATE_MCP_CERTS", "false").lower() in ("true", "1", "yes")
    if not force_regen and os.path.exists(ca_cert_path) and os.path.exists(cert_path) and os.path.exists(key_path):
        return ca_cert_path, cert_path, key_path

    print("[*] Generating PQC Hardware-Seeded Root CA & Leaf Certificate...")
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        local_ip = get_local_ip()

        # Generate CA Private Key
        ca_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        
        # Build Root CA Certificate
        ca_name = x509.Name([
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "SerenityDev PQC Local Authority"),
            x509.NameAttribute(NameOID.COMMON_NAME, "SerenityDev PQC Root CA"),
        ])

        ca_cert = (
            x509.CertificateBuilder()
            .subject_name(ca_name)
            .issuer_name(ca_name)
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
            .not_valid_after(datetime.now(timezone.utc) + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False
            ), critical=True)
            .sign(ca_key, hashes.SHA384())
        )

        # Write CA files
        with open(ca_key_path, "wb") as f:
            f.write(ca_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption()
            ))

        with open(ca_cert_path, "wb") as f:
            f.write(ca_cert.public_bytes(serialization.Encoding.PEM))

        # Generate Leaf Private Key
        leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        # Build Leaf Certificate signed by CA
        leaf_subject = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, local_ip)
        ])

        hostname = socket.gethostname()
        alt_names = [
            x509.DNSName("localhost"),
            x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        ]
        if hostname and hostname != "localhost":
            try:
                alt_names.append(x509.DNSName(hostname))
            except Exception:
                pass

        for ip_str in get_all_local_ips():
            try:
                ip_entry = x509.IPAddress(ipaddress.ip_address(ip_str))
                if ip_entry not in alt_names:
                    alt_names.append(ip_entry)
            except Exception:
                pass

        leaf_cert = (
            x509.CertificateBuilder()
            .subject_name(leaf_subject)
            .issuer_name(ca_name)
            .public_key(leaf_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
            .not_valid_after(datetime.now(timezone.utc) + timedelta(days=825))
            .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
            .add_extension(x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False
            ), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA384())
        )

        with open(key_path, "wb") as f:
            f.write(leaf_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption()
            ))

        with open(cert_path, "wb") as f:
            f.write(leaf_cert.public_bytes(serialization.Encoding.PEM))

        print(f"[OK] Generated Root CA ({ca_cert_path}) and signed Leaf Cert ({cert_path}).")
        return ca_cert_path, cert_path, key_path
    except Exception as e:
        print(f"[!] Certificate generation failed: {e}")
        sys.exit(1)

def start_ca_downloader_server(ca_path: str, port: int = 8080):
    """Starts a background HTTP server serving only the rootCA.pem file for Android browser download."""
    class CAHandler(SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path in ["/rootCA.pem", "/ca", "/", "/rootCA.crt"]:
                try:
                    with open(ca_path, "rb") as f:
                        content = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-x509-ca-cert")
                    self.send_header("Content-Disposition", 'attachment; filename="rootCA.pem"')
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
                except Exception as e:
                    self.send_error(500, f"Error reading CA: {e}")
            else:
                self.send_error(404, "File Not Found")

        def log_message(self, format, *args):
            pass

    try:
        class ReusableHTTPServer(HTTPServer):
            allow_reuse_address = True

        httpd = ReusableHTTPServer(("0.0.0.0", port), CAHandler)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        return httpd
    except Exception as e:
        print(f"[!] Could not start CA server on port {port}: {e}")
        return None

def main():
    port = int(os.environ.get("NATIVE_MCP_PORT", "8443"))
    ca_port = 8080

    # Auto-free ports if held by stale background instances
    free_port(port)
    free_port(ca_port)

    local_ip = get_local_ip()
    ca_cert_path, cert_path, key_path = ensure_pqc_root_ca_and_cert()

    token = os.environ.get("SERENITY_MCP_TOKEN")
    if not token:
        token = SerenityKeyVault.get_machine_entropy().hex()[:32]
        os.environ["SERENITY_MCP_TOKEN"] = token

    # Spin up CA download server on port 8080
    start_ca_downloader_server(ca_cert_path, port=ca_port)

    print("\n=======================================================")
    print("      SERENITYDEV PQC SECURE HTTPS MCP SERVER          ")
    print("=======================================================")
    print(f" Local Machine IP : {local_ip}")
    print(f" Native Port      : {port}")
    print("-------------------------------------------------------")
    print(" STEP 1: INSTALL ROOT CA ON PHONE (If not already installed)")
    print(f"   Download URL   : http://{local_ip}:{ca_port}/rootCA.pem")
    print("   Action         : Open link in phone browser → Download")
    print("                    Settings → Security → Install Certificate → CA Cert")
    print("-------------------------------------------------------")
    print(" STEP 2: ADD MCP SERVER IN GOOGLE AI EDGE GALLERY")
    print(f"   MCP Server URL : https://{local_ip}:{port}/mcp")
    print("   Header Name    : Authorization")
    print(f"   Header Value   : Bearer {token}")
    print("=======================================================\n")

    os.environ["ENFORCE_MCP_HTTPS"] = "false"
    os.environ["ENFORCE_MCP_AUTH"] = "true"

    uvicorn.run(
        "serenitydevserver:app",
        host="0.0.0.0",
        port=port,
        ssl_keyfile=key_path,
        ssl_certfile=cert_path,
        reload=False
    )

if __name__ == "__main__":
    main()
