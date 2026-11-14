"""LAN helpers: find this computer's addresses, pick a free port, make the
HTTPS certificate, and limit access to chosen networks / VLANs."""
import datetime
import ipaddress
import json
import os
import socket
import subprocess
import sys
from pathlib import Path


def primary_ip():
    """The address this computer uses on its main network."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))     # no packet is sent
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def local_ips():
    """Every IPv4 address of this computer (one per network card / VLAN)."""
    ips = {primary_ip()}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(i for i in ips if not i.startswith("127."))


def free_port(host, start, tries=50):
    """`start` if nobody uses it, otherwise the next free port after it."""
    for port in range(start, start + tries):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((host, port))
            return port
        except OSError:
            continue
        finally:
            s.close()
    raise RuntimeError(f"No free port between {start} and {start + tries - 1}")


def sticky_port(host, wanted, memo):
    """The app keeps ONE port. The first start takes `wanted` (or the next
    free one) and writes it to the file `memo`; every later start uses that
    same port, so the address never changes. If the port is taken then,
    the app says so instead of quietly moving to another one."""
    saved = None
    try:
        saved = int(memo.read_text().strip())
    except (OSError, ValueError):
        pass
    if saved:
        try:
            return free_port(host, saved, tries=1)
        except RuntimeError:
            raise RuntimeError(
                f"Port {saved} is in use. FaceFind is probably already running in another "
                f"window - close that window and start again. (To let the app choose a new "
                f"port, delete the file {memo}.)")
    else:
        port = free_port(host, wanted)
        try:
            memo.write_text(str(port))
        except OSError:
            pass
        return port


def parse_networks(text):
    """'192.168.10.0/24, 192.168.20.0/24' -> list of networks. Empty = all."""
    nets = []
    for part in (text or "").replace(";", ",").split(","):
        part = part.strip()
        if part:
            nets.append(ipaddress.ip_network(part, strict=False))
    return nets


def ip_allowed(addr, nets):
    if not nets:
        return True
    try:
        a = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if a.version == 6 and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_loopback or any(a.version == n.version and a in n for n in nets)


def ensure_cert(folder, extra_names=()):
    """Self-signed HTTPS certificate covering every LAN address and name of
    this computer. Made again automatically when an address changes."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    cert_f, key_f, names_f = folder / "cert.pem", folder / "key.pem", folder / "names.json"
    names = sorted({"localhost", "127.0.0.1", socket.gethostname(), *local_ips(),
                    *[n.strip() for n in extra_names if n.strip()]})
    if cert_f.exists() and key_f.exists() and names_f.exists():
        try:
            if json.loads(names_f.read_text()) == {"v": 2, "names": names}:
                return cert_f, key_f
        except ValueError:
            pass
    key = ec.generate_private_key(ec.SECP256R1())
    san = []
    for n in names:
        try:
            san.append(x509.IPAddress(ipaddress.ip_address(n)))
        except ValueError:
            san.append(x509.DNSName(n))
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "FaceFind LAN")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(subject).issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=397))
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
                           critical=False)
            .sign(key, hashes.SHA256()))
    key_f.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    try:
        key_f.chmod(0o600)
    except OSError:
        pass
    cert_f.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    names_f.write_text(json.dumps({"v": 2, "names": names}))
    (folder / "trusted").unlink(missing_ok=True)
    return cert_f, key_f


def offer_trust(cert_f):
    """Windows only, asked once: add the app's own certificate to this
    user's trusted list so the browser on THIS computer shows no warning.
    The certificate can only vouch for this app, nothing else."""
    marker = Path(cert_f).parent / "trusted"
    if os.name != "nt" or marker.exists() or not sys.stdin.isatty():
        return
    print("\nThe browser will warn 'connection is not private' because the")
    print("HTTPS certificate is made by this app.")
    a = input("Remove that warning on THIS computer? Windows will ask - press Yes. [Y/n]: ")
    if a.strip().lower().startswith("n"):
        marker.write_text("declined")
        return
    try:
        r = subprocess.run(["certutil", "-user", "-addstore", "Root", str(cert_f)],
                           capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            marker.write_text("yes")
            print("Done. Close and reopen the browser if it still warns.")
            return
    except Exception:
        pass
    print("Could not do it automatically. In the browser press Advanced > Proceed.")
