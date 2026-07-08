"""安全原语：API key 对称加密（AES-256-GCM）。

evolution 桌面化 + API 收口设计（2026-07-07）：evolution 自建独立加密，
不依赖 executor 的 security.py（决策点 3，选 A）。逻辑与 executor 对齐——
AES-256-GCM 认证加密，主密钥从 settings.evolution_master_key 加载。

主密钥要求 32 字节（256 bit），以 hex（64 字符）或 urlsafe-base64 形式配置。
生成：python -c "import secrets; print(secrets.token_hex(32))"
"""

from __future__ import annotations

import base64
import os
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# AES-GCM nonce 长度（字节）
_GCM_NONCE_LEN = 12


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
        "evolution_master_key must be 32 bytes, encoded as hex (64 chars) or "
        f"urlsafe-base64; got {len(raw)} chars"
    )


def generate_master_key() -> str:
    """生成一个新主密钥（hex），供初始化参考。"""
    return secrets.token_hex(32)


# ── API key 加密（AES-256-GCM）──────────────────────────────
# 存储格式（落到单列）：nonce(12B) || ciphertext+tag，整体 urlsafe-base64。

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
