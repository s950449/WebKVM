import base64
import hashlib
import hmac
import os
import re
import secrets
import socket
import ssl
import subprocess
import time

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1
MAX_FAILS, FAIL_WINDOW = 5, 300


def hash_password(pw):
    salt = os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    b64 = base64.b64encode
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${b64(salt).decode()}${b64(h).decode()}"


def verify_password(pw, stored):
    try:
        _, n, r, p, salt, h = stored.strip().split("$")
        expect = base64.b64decode(h)
        got = hashlib.scrypt(pw.encode(), salt=base64.b64decode(salt),
                             n=int(n), r=int(r), p=int(p), dklen=len(expect))
    except ValueError:
        return False
    return hmac.compare_digest(got, expect)


def write_password(data_dir, pw):
    p = data_dir / "password"
    p.write_text(hash_password(pw) + "\n")
    p.chmod(0o600)


def load_or_init_password(data_dir):
    """讀取密碼雜湊；第一次啟動時產生隨機密碼並回傳明文以便顯示。"""
    p = data_dir / "password"
    if p.exists():
        return p.read_text().strip(), None
    pw = secrets.token_urlsafe(9)
    write_password(data_dir, pw)
    return p.read_text().strip(), pw


class Sessions:
    def __init__(self, hours):
        self.ttl = hours * 3600
        self._tokens = {}

    def create(self):
        token = secrets.token_urlsafe(32)
        self._tokens[token] = time.time() + self.ttl
        return token

    def valid(self, token):
        exp = self._tokens.get(token) if token else None
        if exp is None:
            return False
        if exp < time.time():
            del self._tokens[token]
            return False
        return True

    def revoke(self, token):
        self._tokens.pop(token, None)


class LoginLimiter:
    """同一 IP 在 5 分鐘內失敗 5 次就暫時鎖定。"""

    def __init__(self):
        self._fails = {}

    def _recent(self, ip):
        now = time.time()
        fails = [t for t in self._fails.get(ip, []) if now - t < FAIL_WINDOW]
        self._fails[ip] = fails
        return fails

    def blocked(self, ip):
        return len(self._recent(ip)) >= MAX_FAILS

    def fail(self, ip):
        self._recent(ip).append(time.time())

    def reset(self, ip):
        self._fails.pop(ip, None)


def _local_ips():
    ips = {"127.0.0.1"}
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"],
                             capture_output=True, text=True, timeout=5).stdout
        ips.update(re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", out))
    except (OSError, subprocess.TimeoutExpired):
        pass
    return sorted(ips)


def ssl_context(data_dir, cert="", key=""):
    if not (cert and key):
        cert, key = data_dir / "cert.pem", data_dir / "key.pem"
        if not (cert.exists() and key.exists()):
            san = ",".join(["DNS:localhost", f"DNS:{socket.gethostname()}"]
                           + [f"IP:{ip}" for ip in _local_ips()])
            subprocess.run(["openssl", "req", "-x509", "-newkey", "ec",
                            "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                            "-days", "3650", "-subj", "/CN=webkvm",
                            "-addext", f"subjectAltName={san}",
                            "-keyout", str(key), "-out", str(cert)],
                           check=True, capture_output=True)
            key.chmod(0o600)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cert, key)
    return ctx
