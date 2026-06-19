"""安全原语：密码哈希（scrypt）+ API key 对称加密（AES-256-GCM）。

选型说明：
- 密码哈希用 cryptography 自带的 scrypt（KDF），避免引入 argon2-cffi 额外依赖。
  scrypt 是 OWASP 认可的密码哈希算法，参数对齐 OWASP 推荐值。
- API key 加密用 AES-256-GCM（认证加密），主密钥从 settings.master_key 加载。
- 主密钥要求 32 字节（256 bit），以 hex（64 字符）或 urlsafe-base64 形式配置。

D3 决策：主密钥放 .env（环境变量路线），进程启动加载，靠 OS 文件权限保护。
"""

from __future__ import annotations

import base64
import os
import secrets

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

# scrypt 参数（OWASP 推荐基线：N=2^14, r=8, p=1）
_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32  # 派生密钥长度

# AES-GCM nonce 长度（字节）
_GCM_NONCE_LEN = 12


# ── 主密钥解析 ──────────────────────────────────────────────

def load_master_key(raw: str) -> bytes:
    """把 .env 里的 master_key（hex 或 urlsafe-base64）解析为 32 字节密钥。

    Raises:
        ValueError: 长度/格式不符。
    """
    raw = raw.strip()
    # 先试 hex
    try:
        key = bytes.fromhex(raw)
        if len(key) == 32:
            return key
    except ValueError:
        pass
    # 再试 urlsafe-base64
    try:
        key = base64.urlsafe_b64decode(raw)
        if len(key) == 32:
            return key
    except (ValueError, base64.binascii.Error):
        pass
    raise ValueError(
        "master_key must be 32 bytes, encoded as hex (64 chars) or "
        f"urlsafe-base64; got {len(raw)} chars"
    )


def generate_master_key() -> str:
    """生成一个新主密钥（hex），供初始化参考。"""
    return secrets.token_hex(32)


# ── 密码哈希（scrypt）────────────────────────────────────────
# 存储格式：scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>
# 与 Django 风格类似，便于迁移与校验。

def hash_password(password: str) -> str:
    """scrypt 哈希密码，返回可存储的字符串。"""
    salt = os.urandom(16)
    derived = _scrypt_derive(password.encode("utf-8"), salt)
    return (
        f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}"
        f"${base64.b64encode(salt).decode()}"
        f"${base64.b64encode(derived).decode()}"
    )


def verify_password(password: str, stored: str) -> bool:
    """校验密码是否匹配存储的哈希。使用恒定时间比较防侧信道。"""
    try:
        algo, n_str, r_str, p_str, salt_b64, hash_b64 = stored.split("$")
    except ValueError:
        return False
    if algo != "scrypt":
        return False
    salt = base64.b64decode(salt_b64)
    expected = base64.b64decode(hash_b64)
    derived = _scrypt_derive(
        password.encode("utf-8"), salt,
        n=int(n_str), r=int(r_str), p=int(p_str), dklen=len(expected),
    )
    return secrets.compare_digest(derived, expected)


def _scrypt_derive(
    password: bytes, salt: bytes,
    *, n: int = _SCRYPT_N, r: int = _SCRYPT_R, p: int = _SCRYPT_P,
    dklen: int = _SCRYPT_DKLEN,
) -> bytes:
    kdf = Scrypt(salt=salt, length=dklen, n=n, r=r, p=p)
    return kdf.derive(password)


# ── API key 加密（AES-256-GCM）──────────────────────────────
# 存储格式（落到单列）：nonce(12B) || ciphertext+tag，整体 urlsafe-base64。
# 设计文档 D3 写的是分 iv/tag/tag 三列；实际 GCM 的 tag 会自动附在密文末尾，
# 单列存储更简单且等价安全。这里采用单列方案，简化 schema。

def encrypt_secret(plaintext: str, master_key: bytes) -> str:
    """AES-256-GCM 加密。返回 urlsafe-base64 编码的 nonce||ciphertext+tag。"""
    nonce = os.urandom(_GCM_NONCE_LEN)
    aesgcm = AESGCM(master_key)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    blob = nonce + ciphertext
    return base64.urlsafe_b64encode(blob).decode()


def decrypt_secret(stored: str, master_key: bytes) -> str:
    """AES-256-GCM 解密。密钥错误或密文损坏会抛异常。"""
    blob = base64.urlsafe_b64decode(stored.encode())
    nonce, ciphertext = blob[:_GCM_NONCE_LEN], blob[_GCM_NONCE_LEN:]
    aesgcm = AESGCM(master_key)
    return aesgcm.decrypt(nonce, ciphertext, None).decode("utf-8")
