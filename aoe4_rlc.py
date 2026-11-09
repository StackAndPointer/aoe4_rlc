#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
《帝国时代 IV》 --  本地存档（LuaDataStore .rlc）查看 / 编辑工具
================================================================
用于查看和调整自己电脑上单机存档 rogue_mode.rlc 中的数值字段。
只读写游戏已经生成的存档文件：不修改游戏本体，不联网，不涉及任何在线校验。

结构：
    aoe4_rlc_core.py     核心库：存档格式的解析与加解密（纯函数，无界面/网络/日志）
    aoe4_rlc_server.py   后端服务：JSON REST API + 托管前端静态文件
    web/                 前端：index.html / style.css / app.js（浏览器里跑）
    aoe4_rlc.py          本文件：统一入口（命令行 + 启动服务）

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

【依赖】pip install pycryptodome    (CityHash64 已内置纯 Python 实现)

【让修改生效】游戏进度走 Steam 云同步，云端那份可能覆盖本地文件。请在
  Steam -> 库 -> 右键游戏 -> 「属性」->「启动选项」里填入 -noCloudSaves，
  并在保存前完全退出游戏。

【用法】
  python aoe4_rlc.py                                      启动后端并打开浏览器
  python aoe4_rlc.py --serve [--port 8765] [--no-browser] 启动后端服务
  python aoe4_rlc.py <账号ID> <文件.rlc> [输出.bin]         解密（命令行）
  python aoe4_rlc.py --encrypt <账号ID> <文件.bin> [输出.rlc] 加密（命令行）
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aoe4_rlc_core as core


def _usage() -> int:
    print(__doc__)
    return 0


def run_cli(args) -> int:
    """命令行加解密（不经过 HTTP）。"""
    encrypt = False
    if args and args[0] == "--encrypt":
        encrypt = True
        args = args[1:]

    if len(args) < 2:
        return _usage()

    account, src = args[0], args[1]
    dst = args[2] if len(args) > 2 else None

    if encrypt:
        with open(src, "rb") as f:
            plain = f.read()
        blob = core.encrypt_blob(plain, account)
        if dst:
            with open(dst, "wb") as f:
                f.write(blob)
            print("已加密写出 %s (%d -> %d 字节)" % (dst, len(plain), len(blob)))
        else:
            sys.stdout.buffer.write(blob)
        return 0

    blob, plain = core.read_rlc(src, account)
    ok = b"LuaDataStore" in plain[:64]
    if dst:
        with open(dst, "wb") as f:
            f.write(plain)
        print("已解密写出 %s (%d -> %d 字节)%s"
              % (dst, len(blob), len(plain),
                 "" if ok else "  [警告] 未看到 LuaDataStore 标记"))
    else:
        sys.stdout.write(plain.decode("latin1"))
    return 0


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if not args:
        import aoe4_rlc_server as server
        return server.serve()

    head = args[0]
    if head in ("-h", "--help"):
        return _usage()

    if head in ("--serve", "--web", "--gui", "--gui-open"):
        import aoe4_rlc_server as server
        return server.main(args[1:])

    if head.startswith("--port") or head.startswith("-p"):
        import aoe4_rlc_server as server
        return server.main(args)

    return run_cli(args)


if __name__ == "__main__":
    sys.exit(main())
