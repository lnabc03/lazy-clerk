"""RSA-1024 / PKCS#1 v1.5 密码加密，复现登录页 JsEncryptHelper.js（JSEncrypt）行为。

公钥硬编码于目标登录页前端；PKCS#1 v1.5 加密结果每次随机，与浏览器一致。
"""
from __future__ import annotations

import base64

from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA

PUBLIC_KEY_B64 = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCC0hrRIjb3noDWNtbDpANbjt5Iwu2NFeDw"
    "U16Ec87ToqeoIm2KI+cOs81JP9aTDk/jkAlU97mN8wZkEMDr5utAZtMVht7GLX33Wx9Xjq"
    "xUsDfsGkqNL8dXJklWDu9Zh80Ui2Ug+340d5dZtKtd+nv09QZqGjdnSp9PTfFDBY133QIDAQAB"
)

_KEY = RSA.import_key(base64.b64decode(PUBLIC_KEY_B64))


def encrypt_password(plain: str) -> str:
    cipher = PKCS1_v1_5.new(_KEY)
    return base64.b64encode(cipher.encrypt(plain.encode())).decode()
