"""S0 单测的 pytest 形式：RSA-1024 密文 128 字节、每次加密结果随机。"""
import base64

from app.core.crypto import encrypt_password


def test_rsa_ciphertext_length():
    assert len(base64.b64decode(encrypt_password("any-password"))) == 128


def test_rsa_randomized():
    assert encrypt_password("same") != encrypt_password("same")
