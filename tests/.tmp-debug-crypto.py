"""临时调试脚本：验证 videoeditor.dll 的 encrypt/decrypt 配对行为。"""
import base64
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cutwright.backends.jianying import crypto as C  # noqa: E402

drafts = r"D:\Jianying\JianyingPro Drafts"
sample = os.path.join(drafts, "10月18日", "draft_content.json")
print("dll dir :", C.find_install_dir())
print("available:", C.available())

c = C.get_crypto()
print("has_flag:", c._has_flag, "enabled:", c.is_enabled)

if os.path.isfile(sample):
    raw = open(sample, "rb").read()
    print("sample bytes:", len(raw), "head:", raw[:16])
    try:
        plain = c.decrypt_bytes(raw)
        print("decrypt sample OK:", len(plain), plain[:60])
    except Exception as exc:
        print("decrypt sample FAIL:", type(exc).__name__, exc)
    # 再加密回去，比对长度
    try:
        enc = c.encrypt_bytes(plain)
        print("re-encrypt:", len(enc), enc[:24])
        back = c.decrypt_bytes(enc)
        print("roundtrip equal:", back == plain)
    except Exception as exc:
        print("re-encrypt FAIL:", type(exc).__name__, exc)

plain2 = json.dumps({"id": "ABCDEF", "new_version": "189.0.0",
                     "tracks": [], "duration": 1000000}).encode()
print("tiny plain:", len(plain2))
c.enable(True)
keep = []
src = C._make_in(plain2, keep)
out = C._make_out()
c._encrypt(None, __import__("ctypes").byref(out), __import__("ctypes").byref(src))
enc2 = C._read_out(out)
print("tiny enc:", len(enc2), enc2[:40])
print("is base64 ascii:", all(32 <= b < 127 for b in enc2))
try:
    dec2 = c.decrypt_bytes(enc2)
    print("tiny roundtrip:", dec2 == plain2, len(dec2))
except Exception as exc:
    print("tiny decrypt FAIL:", type(exc).__name__, exc)
    # 尝试 base64 解码后再解
    try:
        dec3 = c.decrypt_bytes(base64.b64decode(enc2))
        print("  after b64decode:", dec3 == plain2, len(dec3))
    except Exception as exc2:
        print("  after b64decode FAIL:", type(exc2).__name__, exc2)
