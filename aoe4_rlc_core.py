#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aoe4_rlc_core.py  --  后端核心（无界面、无网络、无日志）
=======================================================
《帝国时代 IV》本地单机存档（LuaDataStore .rlc）的读写与解析。

本模块只处理用户自己电脑上、由游戏正常生成的存档文件：不修改游戏本体，
不联网，不涉及任何在线校验。用途限于单机场景下查看和调整自己的存档数据。

【存档格式】
  文件   = AES-256-CBC 密文 + PKCS#7 填充
  密钥   = EVP_BytesToKey(SHA-1, salt, 迭代 4 次)
           输入 = CityHash64(账号ID)[8 字节小端] + 固定尾部常量(23 字节)
  账号ID = SteamID64 十进制字符串（即 Users\\<SteamID64>\\ 目录名）

  明文   = u32 头部字段(含义未确认) | u8 版本 | 分块流
  分块   = tag(u32) + 负载
           tag 4 = 字符串  {u32 长度, 字节}
           tag 3 = 数值    {u8 子类型, i64}
           其余为表标记等

本模块只依赖 pycryptodome 与标准库，可被 CLI / HTTP 服务直接复用。
"""

from __future__ import annotations

import hashlib
import os
import re
import struct

try:
    from Crypto.Cipher import AES
except ImportError:  # pragma: no cover
    AES = None

# --------------------------------------------------------------------------
# 存档格式常量（密钥派生所需）
# --------------------------------------------------------------------------
KEY_TAIL = b"key was not right size\x00"      # 密钥材料固定尾部，23 字节
SALT = bytes.fromhex("72140000c9280000")      # 密钥派生盐值，8 字节
KDF_COUNT = 4                                 # EVP_BytesToKey 迭代次数
APP_VERSION = "r6-2155"


# --------------------------------------------------------------------------
# CityHash64 (纯 Python, 与 C++ 参考实现逐位一致)
# --------------------------------------------------------------------------
_M64 = (1 << 64) - 1
_k0 = 0xC3A5C85C97CB3127
_k1 = 0xB492B66FBE98F273
_k2 = 0x9AE16A3B2F90404F


def _f64(s: bytes, i: int) -> int:
    return int.from_bytes(s[i:i + 8], "little")


def _f32(s: bytes, i: int) -> int:
    return int.from_bytes(s[i:i + 4], "little")


def _rot(v: int, shift: int) -> int:
    v &= _M64
    return ((v >> shift) | (v << (64 - shift))) & _M64 if shift else v


def _bswap64(v: int) -> int:
    return int.from_bytes((v & _M64).to_bytes(8, "little"), "big")


def _shiftmix(v: int) -> int:
    return (v ^ (v >> 47)) & _M64


def _hashlen16(u: int, v: int, mul: int) -> int:
    a = ((u ^ v) * mul) & _M64
    a ^= a >> 47
    b = ((v ^ a) * mul) & _M64
    b ^= b >> 47
    return (b * mul) & _M64


def _hashlen0to16(s: bytes) -> int:
    n = len(s)
    if n >= 8:
        mul = (_k2 + n * 2) & _M64
        a = (_f64(s, 0) + _k2) & _M64
        b = _f64(s, n - 8)
        c = (_rot(b, 37) * mul + a) & _M64
        d = ((_rot(a, 25) + b) * mul) & _M64
        return _hashlen16(c, d, mul)
    if n >= 4:
        mul = (_k2 + n * 2) & _M64
        a = _f32(s, 0)
        return _hashlen16((n + (a << 3)) & _M64, _f32(s, n - 4), mul)
    if n > 0:
        a, b, c = s[0], s[n >> 1], s[n - 1]
        y = a + (b << 8)
        z = n + (c << 2)
        return (_shiftmix((((y * _k2) & _M64) ^ ((z * _k0) & _M64))) * _k2) & _M64
    return _k2


def _hashlen17to32(s: bytes) -> int:
    n = len(s)
    mul = (_k2 + n * 2) & _M64
    a = (_f64(s, 0) * _k1) & _M64
    b = _f64(s, 8)
    c = (_f64(s, n - 8) * mul) & _M64
    d = (_f64(s, n - 16) * _k2) & _M64
    return _hashlen16((_rot((a + b) & _M64, 43) + _rot(c, 30) + d) & _M64,
                      (a + _rot((b + _k2) & _M64, 18) + c) & _M64, mul)


def _hashlen33to64(s: bytes) -> int:
    n = len(s)
    mul = (_k2 + n * 2) & _M64
    a = (_f64(s, 0) * _k2) & _M64
    b = _f64(s, 8)
    c = _f64(s, n - 24)
    d = _f64(s, n - 32)
    e = (_f64(s, 16) * _k2) & _M64
    f = (_f64(s, 24) * 9) & _M64
    g = _f64(s, n - 8)
    h = (_f64(s, n - 16) * mul) & _M64

    u = (_rot((a + g) & _M64, 43) + (_rot(b, 30) + c) * 9) & _M64
    v = ((((a + g) & _M64) ^ d) + f + 1) & _M64
    w = (_bswap64((((u + v) & _M64) * mul) & _M64) + h) & _M64
    x = (_rot((e + f) & _M64, 42) + c) & _M64
    y = (((_bswap64((((v + w) & _M64) * mul) & _M64) + g) & _M64) * mul) & _M64
    z = (e + f + c) & _M64

    t = ((((x + z) & _M64) * mul) & _M64)
    a2 = (_bswap64((t + y) & _M64) + b) & _M64
    t2 = ((((z + a2) & _M64) * mul) & _M64)
    b2 = (_shiftmix((t2 + d + h) & _M64) * mul) & _M64
    return (b2 + x) & _M64


def _weak_hash_len32_with_seeds(data: bytes, off: int, a: int, b: int):
    w = _f64(data, off)
    x = _f64(data, off + 8)
    y = _f64(data, off + 16)
    z = _f64(data, off + 24)
    a = (a + w) & _M64
    b = _rot((b + a + z) & _M64, 21)
    c = a
    a = (a + x + y) & _M64
    b = (b + _rot(a, 44)) & _M64
    return (a + z) & _M64, (b + c) & _M64


def cityhash64(data: bytes) -> int:
    """CityHash64, 覆盖 0..64 字节 (账号 ID 一定落在此范围内)。"""
    n = len(data)
    if n <= 16:
        return _hashlen0to16(data)
    if n <= 32:
        return _hashlen17to32(data)
    if n <= 64:
        return _hashlen33to64(data)
    x = _f64(data, n - 40)
    y = (_f64(data, n - 16) + _f64(data, n - 56)) & _M64
    z = _hashlen16((_f64(data, n - 48) + n) & _M64, _f64(data, n - 24), _k2)
    v = _weak_hash_len32_with_seeds(data, n - 64, n, z)
    w = _weak_hash_len32_with_seeds(data, n - 32, (y + _k1) & _M64, x)
    x = (x * _k1 + _f64(data, 0)) & _M64
    pos = 0
    length = (n - 1) & ~63
    while length:
        x = (_rot((x + y + v[0] + _f64(data, pos + 8)) & _M64, 37) * _k1) & _M64
        y = (_rot((y + v[1] + _f64(data, pos + 48)) & _M64, 42) * _k1) & _M64
        x ^= w[1]
        y = (y + v[0] + _f64(data, pos + 40)) & _M64
        z = (_rot((z + w[0]) & _M64, 33) * _k1) & _M64
        v = _weak_hash_len32_with_seeds(data, pos, (v[1] * _k1) & _M64,
                                        (x + w[0]) & _M64)
        w = _weak_hash_len32_with_seeds(data, pos + 32, (z + w[1]) & _M64,
                                        (y + _f64(data, pos + 16)) & _M64)
        z, x = x, z
        pos += 64
        length -= 64
    return _hashlen16((_hashlen16(v[0], w[0], _k1) + _shiftmix(y) * _k1 + z) & _M64,
                      (_hashlen16(v[1], w[1], _k1) + x) & _M64, _k1)


# --------------------------------------------------------------------------
# 密钥派生 / 加解密
# --------------------------------------------------------------------------
def evp_bytestokey(data: bytes, count: int = KDF_COUNT,
                   klen: int = 32, ivlen: int = 16):
    """OpenSSL EVP_BytesToKey(md=SHA1, salt=SALT)。"""
    out, prev = b"", b""
    while len(out) < klen + ivlen:
        h = hashlib.sha1(prev + data + SALT).digest()
        for _ in range(count - 1):
            h = hashlib.sha1(h).digest()
        out += h
        prev = h
    return out[:klen], out[klen:klen + ivlen]


def derive_keys(account_id) -> tuple[bytes, bytes]:
    seed = cityhash64(str(account_id).encode("utf-8"))
    material = struct.pack("<Q", seed) + KEY_TAIL
    return evp_bytestokey(material)


def _require_aes():
    if AES is None:
        raise RuntimeError("缺少依赖：请先执行  pip install pycryptodome")


def decrypt_blob(blob: bytes, account_id) -> bytes:
    _require_aes()
    key, iv = derive_keys(account_id)
    plain = AES.new(key, AES.MODE_CBC, iv).decrypt(blob)
    n = plain[-1] if plain else 0
    if 1 <= n <= 16 and plain[-n:] == bytes([n]) * n:
        plain = plain[:-n]
    return plain


def encrypt_blob(plain, account_id) -> bytes:
    _require_aes()
    plain = bytes(plain)
    key, iv = derive_keys(account_id)
    n = 16 - (len(plain) % 16)
    return AES.new(key, AES.MODE_CBC, iv).encrypt(plain + bytes([n]) * n)


# --------------------------------------------------------------------------
# 明文解析：只做「局部模式匹配」，不依赖完整表语法 -> 字节级无损
# --------------------------------------------------------------------------
NUM_REC = 13          # tag(4) + 子类型(1) + i64(8)


class Entry:
    """一个可编辑字段。offset 指向它可变部分在明文中的位置。"""
    __slots__ = ("kind", "key", "offset", "size", "value", "path", "depth")

    def __init__(self, kind, key, offset, size, value, path, depth):
        self.kind = kind          # 'num' | 'str' | 'key'
        self.key = key
        self.offset = offset
        self.size = size
        self.value = value
        self.path = path
        self.depth = depth

    def __repr__(self):
        return f"<{self.kind} {self.path}={self.value!r} @{self.offset}>"


def _u32(b, o: int) -> int:
    return struct.unpack_from("<I", b, o)[0]


def _printable(b: bytes) -> bool:
    return bool(b) and all(32 <= c < 127 for c in b)


def parse_entries(pt) -> list[Entry]:
    """扫描明文, 提取可编辑字段（平面顺序, 字节精确）。

    识别两种紧随 key 的形态:
        key(4,len,bytes) + num(3,sub,i64)   -> 数值
        key(4,len,bytes) + str(4,len,bytes) -> 字符串
    其余标记跳过。不做全局遍历推断层级, 因此不会因解析失步而破坏文件。
    """
    entries: list[Entry] = []
    n = len(pt)
    i = 0
    while i + 8 <= n:
        if _u32(pt, i) == 4:
            ln = _u32(pt, i + 4)
            if 0 < ln <= 512 and i + 8 + ln <= n and _printable(pt[i + 8:i + 8 + ln]):
                text = pt[i + 8:i + 8 + ln].decode("latin1")
                j = i + 8 + ln
                if j + NUM_REC <= n and _u32(pt, j) == 3:
                    val = struct.unpack_from("<q", pt, j + 5)[0]
                    entries.append(Entry("num", text, j + 5, 8, val, text, 0))
                    i = j + NUM_REC
                    continue
                if j + 8 <= n and _u32(pt, j) == 4:
                    l2 = _u32(pt, j + 4)
                    if 0 < l2 <= 512 and j + 8 + l2 <= n and _printable(pt[j + 8:j + 8 + l2]):
                        v = pt[j + 8:j + 8 + l2].decode("latin1")
                        entries.append(Entry("str", text, j + 8, l2, v, text, 0))
                        i = j + 8 + l2
                        continue
                entries.append(Entry("key", text, i, ln, None, text, 0))
                i = j
                continue
        i += 1
    return apply_schema(entries)


# ---------------------------------------------------------------------------
# 人工核定的归属表（只影响显示层级，不影响任何字节偏移）
# ---------------------------------------------------------------------------
_PARENT_RULES = (
    (r"^c\d+$", "boons"),                 # 恩惠/祝福
    (r"^p\d+$", "completedChallenges"),   # 已完成挑战
    (r"^n\d+$", "perks"),                 # 天赋节点
    (r"^(perkpoint|level|durability)$", "perks"),
)

_TOP_KEYS = ("rogue_mode",)


def apply_schema(entries: list[Entry]) -> list[Entry]:
    """按核定归属表设置 depth / path（仅用于显示）。"""
    favor_seen = 0
    for e in entries:
        k = e.key
        if k == "LuaDataStore":
            e.depth, e.path = 0, k
            continue
        if k in _TOP_KEYS:
            e.depth, e.path = 1, "LuaDataStore." + k
            continue
        parent = None
        for pat, par in _PARENT_RULES:
            if re.match(pat, k):
                parent = par
                break
        if parent is None and k == "favor":
            parent = "header" if favor_seen == 0 else None
            favor_seen += 1
        if parent:
            e.depth, e.path = 3, "LuaDataStore.rogue_mode.%s.%s" % (parent, k)
        else:
            e.depth, e.path = 2, "LuaDataStore.rogue_mode." + k
    return entries


_FIELD_DOC = {
    "LuaDataStore": "存档根节点",
    "rogue_mode": "Rogue 模式进度根",
    "perks": "天赋/遗物区",
    "completedChallenges": "已完成挑战列表",
    "header": "摘要区",
    "boons": "恩惠/祝福列表",
    "ascension": "飞升/晋升进度",
    "favor_pool_conqueror": "「征服者」分类的恩惠池",
    "dailybonustime": "每日奖励计时基准，改小可刷新每日奖励",
    "completedDifficulty": "已完成的最高难度",
    "savetime": "存档时间（毫秒 Unix 时间戳）",
    "version": "存档格式版本，不建议修改",
    "majorversion": "主版本号，不建议修改",
    "favor_cap": "恩惠上限，favor 涨到此值即封顶",
    "compensated": "补偿标记（1 = 该恩惠已被补偿）",
    "perkpoint": "该天赋的点数成本（不是余额）",
    "level": "物品/遗物等级",
    "durability": "耐久度",
}

_PAT_DOC = (
    (r"^c\d+$", "已获得恩惠 ID（具体含义由模组定义）"),
    (r"^p\d+$", "已完成挑战 ID（具体含义由模组定义）"),
    (r"^n\d+$", "天赋节点状态（0 = 未解锁）"),
)


def describe(e: Entry) -> str:
    """返回字段的中文说明。"""
    k = e.key
    if k == "favor":
        if e.path.endswith("header.favor"):
            return "天赋点余额（游戏里看到的总数）"
        return "恩惠余额（另一恩惠池）"
    if k in _FIELD_DOC:
        return _FIELD_DOC[k]
    for pat, txt in _PAT_DOC:
        if re.match(pat, k):
            return txt
    return ""


def get_header(pt):
    """返回 (u32 头部字段, u8 版本)；长度不足返回 None。

    注意：首个 u32 的含义尚未确认。它随每次保存而变化，与保存时间无关，
    也不是任何常见校验和（CRC32 / Adler32 / CityHash / 长度都对不上）。
    真正的存档时间在明文里的 savetime 字段（毫秒级 Unix 时间戳）。
    """
    if len(pt) < 5:
        return None
    return _u32(pt, 0), pt[4]


# --------------------------------------------------------------------------
# 字段树 / 摘要：给前端直接渲染的行模型
# --------------------------------------------------------------------------
def build_rows(entries: list[Entry], plain=None, original=None) -> list[dict]:
    """把 Entry 列表压平成 DFS 顺序的行，供前端树形表格渲染。

    每行: id / parent / depth / kind / key / path / value / doc /
          offset / size / modified / has_children / index
    """
    root = {"kids": {}, "ent": []}
    for idx, e in enumerate(entries):
        node = root
        for part in e.path.split("."):
            node = node["kids"].setdefault(part, {"kids": {}, "ent": []})
        node["ent"].append(idx)

    rows: list[dict] = []

    def entry_row(e: Entry, idx: int, depth: int, parent, has_children: bool):
        modified = False
        if original is not None and plain is not None:
            o, n = e.offset, e.offset + e.size
            modified = bytes(original[o:n]) != bytes(plain[o:n])
        return {
            "id": "e%d" % idx,
            "parent": parent,
            "depth": depth,
            "kind": e.kind,
            "key": e.key,
            "path": e.path,
            "value": e.value,
            "doc": describe(e),
            "offset": e.offset,
            "size": e.size,
            "modified": modified,
            "has_children": has_children,
            "index": idx,
        }

    def walk(node, prefix: str, depth: int, parent):
        for name, child in node["kids"].items():
            path = (prefix + "." + name) if prefix else name
            has_kids = bool(child["kids"])
            anchor = parent
            for idx in child["ent"]:
                row = entry_row(entries[idx], idx, depth, anchor, has_kids)
                rows.append(row)
                anchor = row["id"]
            if has_kids and not child["ent"]:
                gid = "g:" + path
                rows.append({
                    "id": gid,
                    "parent": anchor,
                    "depth": depth,
                    "kind": "group",
                    "key": name,
                    "path": path,
                    "value": None,
                    "doc": "",
                    "offset": None,
                    "size": None,
                    "modified": False,
                    "has_children": True,
                    "index": None,
                })
                anchor = gid
            walk(child, path, depth + 1, anchor)

    walk(root, "", 0, None)
    return rows


def summarize(entries: list[Entry]) -> dict:
    """汇总关键数值，供界面顶部展示。"""
    nums = [e for e in entries if e.kind == "num"]

    def vals(k):
        return [e.value for e in nums if e.key == k]

    out: dict = {}
    favor = vals("favor")
    if favor:
        out["favor"] = favor
    for key in ("favor_cap", "version", "majorversion", "savetime",
                "completedDifficulty", "dailybonustime"):
        v = vals(key)
        if v:
            out[key] = v[0]
    perk = vals("perkpoint")
    if perk:
        out["perkpoint_sum"] = sum(perk)
        out["perkpoint_count"] = len(perk)
    out["count_num"] = len(nums)
    out["count_str"] = sum(1 for e in entries if e.kind == "str")
    out["count_key"] = sum(1 for e in entries if e.kind == "key")
    return out


# --------------------------------------------------------------------------
# 文件级操作
# --------------------------------------------------------------------------
def read_rlc(path: str, account_id):
    """返回 (密文, 明文)。"""
    with open(path, "rb") as f:
        blob = f.read()
    return blob, decrypt_blob(blob, account_id)


def write_rlc(path: str, plain, account_id, backup: bool = True):
    """把明文加密写回 path。返回备份文件路径（若有）。"""
    blob = encrypt_blob(plain, account_id)
    bak = None
    if backup and os.path.exists(path):
        bak = path + ".bak"
        if not os.path.exists(bak):
            with open(path, "rb") as src, open(bak, "wb") as dst:
                dst.write(src.read())
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, path)
    return bak


def users_dir() -> str:
    return os.path.join(os.path.expanduser("~"), "Documents", "My Games",
                        "Age of Empires IV", "Users")


def find_datastore_dir(account_id=None):
    """猜测存档目录。"""
    docs = users_dir()
    if not os.path.isdir(docs):
        return None
    if account_id:
        cand = os.path.join(docs, str(account_id), "datastore")
        if os.path.isdir(cand):
            return cand
    for name in os.listdir(docs):
        cand = os.path.join(docs, name, "datastore")
        if os.path.isdir(cand):
            return cand
    return None


def account_from_path(path: str) -> str:
    """从 ...\\Users\\<SteamID64>\\... 里取出账号 ID。"""
    m = re.search(r"Users[\\/](\d{6,20})[\\/]", path or "", re.IGNORECASE)
    return m.group(1) if m else ""


def list_accounts() -> list[dict]:
    """列出本机所有账号目录（按最近修改排序）。"""
    docs = users_dir()
    out = []
    if not os.path.isdir(docs):
        return out
    for name in sorted(os.listdir(docs)):
        ds = os.path.join(docs, name, "datastore")
        if not os.path.isdir(ds):
            continue
        try:
            mtime = os.path.getmtime(ds)
        except OSError:
            mtime = 0
        out.append({"account": name, "datastore": ds, "mtime": int(mtime)})
    out.sort(key=lambda a: a["mtime"], reverse=True)
    return out


def list_candidates() -> list[dict]:
    """扫描所有账号目录下的 .rlc 存档，供前端「打开」列表使用。"""
    docs = users_dir()
    out = []
    if not os.path.isdir(docs):
        return out
    for name in sorted(os.listdir(docs)):
        ds = os.path.join(docs, name, "datastore")
        if not os.path.isdir(ds):
            continue
        for sub in ("", "cloud"):
            d = os.path.join(ds, sub) if sub else ds
            if not os.path.isdir(d):
                continue
            try:
                files = os.listdir(d)
            except OSError:
                continue
            for fn in sorted(files):
                if not fn.lower().endswith(".rlc"):
                    continue
                p = os.path.join(d, fn)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                out.append({
                    "account": name,
                    "path": p,
                    "name": fn,
                    "cloud": bool(sub),
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                })
    out.sort(key=lambda c: (c["account"], c["name"], c["cloud"]))
    return out
