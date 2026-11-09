#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aoe4_rlc_server.py  --  后端 HTTP 服务（REST API + 静态前端托管）
=================================================================
前后端分离中的「后端」：
  * 只负责加解密、解析、改字段、写盘，不含任何界面代码；
  * 通过 JSON REST 接口对外提供服务；
  * 同时把 web/ 目录下的静态前端托管在同一个端口上；
  * 不写任何日志文件，访问日志也被关闭。

只在本机回环地址上监听，供本地浏览器界面调用，不对外提供网络服务。

启动：python aoe4_rlc_server.py [--port 8765] [--no-browser]

【让修改生效】保存前请先完全退出游戏；并在 Steam -> 库 -> 右键游戏 ->「属性」
->「启动选项」里填入 -noCloudSaves，避免云存档把本地修改覆盖回去。

接口一览
  GET  /api/state            当前会话完整状态（含字段树）
  GET  /api/candidates       扫描本机所有 .rlc 存档
  GET  /api/selftest         自检
  POST /api/open             {path, account}  打开存档
  POST /api/edit             {index, value}   改一个字段（仅内存）
  POST /api/revert           {index}          还原为打开时的值
  POST /api/undo             撤销上一步修改
  POST /api/save             {account}        重新加密写盘 + 回读校验
  POST /api/close            关闭当前存档
  POST /api/export           {kind}           plain | json 导出到源文件旁
"""

from __future__ import annotations

import json
import os
import socket
import struct
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aoe4_rlc_core as core

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
MAX_UNDO = 200


class ApiError(Exception):
    """可以直接展示给前端的错误。"""


# --------------------------------------------------------------------------
# 会话：同一时刻只编辑一个存档
# --------------------------------------------------------------------------
class Session:
    def __init__(self):
        self.lock = threading.RLock()
        self.account = ""
        self.path = None
        self.plain = None          # bytearray，编辑中的明文
        self.original = None       # 打开时的明文，用于「还原」和「是否修改过」
        self.undo = []             # [(offset, old_bytes), ...]

    # ---------------- 状态 ----------------
    def dirty(self) -> bool:
        return (self.plain is not None and self.original is not None
                and bytes(self.plain) != self.original)

    def entries(self):
        return core.parse_entries(self.plain) if self.plain is not None else []

    def state(self) -> dict:
        with self.lock:
            loaded = self.plain is not None
            st = {
                "ok": True,
                "version": core.APP_VERSION,
                "account": self.account,
                "path": self.path,
                "file": os.path.basename(self.path) if self.path else "",
                "loaded": loaded,
                "plain_size": len(self.plain) if loaded else 0,
                "dirty": self.dirty(),
                "undo": len(self.undo),
                "header": None,
                "counts": {"num": 0, "str": 0, "key": 0, "total": 0},
                "summary": {},
                "rows": [],
            }
            if not loaded:
                return st
            hdr = core.get_header(self.plain)
            if hdr:
                st["header"] = {"timestamp": hdr[0], "format": hdr[1]}
            ents = core.parse_entries(self.plain)
            st["counts"] = {
                "num": sum(1 for e in ents if e.kind == "num"),
                "str": sum(1 for e in ents if e.kind == "str"),
                "key": sum(1 for e in ents if e.kind == "key"),
                "total": len(ents),
            }
            st["summary"] = core.summarize(ents)
            st["rows"] = core.build_rows(ents, self.plain, self.original)
            return st

    # ---------------- 打开 ----------------
    def open(self, path: str, account: str = "") -> dict:
        with self.lock:
            path = (path or "").strip().strip('"')
            if not path:
                raise ApiError("请先选择要打开的 .rlc 存档")
            if not os.path.isfile(path):
                raise ApiError("文件不存在：%s" % path)
            acc = (account or "").strip() or core.account_from_path(path)
            if not acc:
                raise ApiError("缺少 SteamID64：请填写 Users\\<ID>\\ 目录里的那串数字")
            try:
                _blob, plain = core.read_rlc(path, acc)
            except Exception as exc:
                raise ApiError("解密失败：%s（最常见的原因是 SteamID64 不对）" % exc)
            self.account = acc
            self.path = os.path.abspath(path)
            self.plain = bytearray(plain)
            self.original = bytes(plain)
            self.undo = []
            st = self.state()
            st["warning"] = None
            if b"LuaDataStore" not in plain[:64]:
                st["warning"] = "解出的内容里没有 LuaDataStore 标记，SteamID64 可能不对。"
            return st

    def close(self) -> dict:
        with self.lock:
            self.path = None
            self.plain = None
            self.original = None
            self.undo = []
            return self.state()

    # ---------------- 编辑 ----------------
    def _push_undo(self, offset: int, old: bytes):
        self.undo.append((offset, old))
        if len(self.undo) > MAX_UNDO:
            del self.undo[0:len(self.undo) - MAX_UNDO]

    def _entry(self, index):
        ents = self.entries()
        if not isinstance(index, int) or index < 0 or index >= len(ents):
            raise ApiError("字段索引越界：%r" % (index,))
        return ents[index]

    def edit(self, index, text) -> dict:
        with self.lock:
            if self.plain is None:
                raise ApiError("还没有打开任何存档")
            e = self._entry(index)
            if e.kind == "key":
                raise ApiError("「%s」是分组名，不能直接修改" % e.key)
            if e.kind == "num":
                raw = str(text).strip()
                if not raw:
                    raise ApiError("请填一个整数")
                try:
                    iv = int(raw, 0)
                except ValueError:
                    raise ApiError("「%s」不是合法整数（可写 1500 或 0x5DC）" % raw)
                if not (-(1 << 63) <= iv < (1 << 63)):
                    raise ApiError("数值超出 64 位有符号整数范围")
                old = struct.unpack_from("<q", self.plain, e.offset)[0]
                if iv == old:
                    return {"ok": True, "changed": False, "old": old, "value": iv,
                            "state": self.state()}
                self._push_undo(e.offset, bytes(self.plain[e.offset:e.offset + 8]))
                struct.pack_into("<q", self.plain, e.offset, iv)
                return {"ok": True, "changed": True, "old": old, "value": iv,
                        "state": self.state()}
            # 字符串
            try:
                new_bytes = str(text).encode("latin1")
            except UnicodeEncodeError:
                raise ApiError("该字段只支持单字节字符（latin1）")
            if len(new_bytes) != e.size:
                raise ApiError("字符串长度必须保持 %d 字节，当前输入 %d 字节"
                               % (e.size, len(new_bytes)))
            old = bytes(self.plain[e.offset:e.offset + e.size]).decode("latin1")
            if old == new_bytes.decode("latin1"):
                return {"ok": True, "changed": False, "old": old, "value": old,
                        "state": self.state()}
            self._push_undo(e.offset, bytes(self.plain[e.offset:e.offset + e.size]))
            self.plain[e.offset:e.offset + e.size] = new_bytes
            return {"ok": True, "changed": True, "old": old,
                    "value": new_bytes.decode("latin1"), "state": self.state()}

    def revert(self, index) -> dict:
        with self.lock:
            if self.plain is None:
                raise ApiError("还没有打开任何存档")
            e = self._entry(index)
            if e.kind == "key":
                raise ApiError("分组名没有可还原的值")
            seg = bytes(self.original[e.offset:e.offset + e.size])
            now = bytes(self.plain[e.offset:e.offset + e.size])
            if seg == now:
                return {"ok": True, "changed": False, "state": self.state()}
            self._push_undo(e.offset, now)
            self.plain[e.offset:e.offset + e.size] = seg
            return {"ok": True, "changed": True, "state": self.state()}

    def undo_step(self) -> dict:
        with self.lock:
            if not self.undo:
                raise ApiError("没有可撤销的修改")
            offset, old = self.undo.pop()
            self.plain[offset:offset + len(old)] = old
            return {"ok": True, "state": self.state()}

    # ---------------- 写盘 ----------------
    def save(self, account: str = "") -> dict:
        with self.lock:
            if self.plain is None:
                raise ApiError("还没有打开任何存档")
            acc = (account or "").strip() or self.account
            if not acc:
                raise ApiError("缺少 SteamID64")
            try:
                bak = core.write_rlc(self.path, self.plain, acc, backup=True)
            except Exception as exc:
                raise ApiError("写入失败：%s" % exc)
            self.account = acc
            self.original = bytes(self.plain)
            self.undo = []
            verify = {"ok": False, "detail": ""}
            try:
                _blob, plain2 = core.read_rlc(self.path, acc)
                if bytes(plain2) == bytes(self.plain):
                    verify = {"ok": True,
                              "detail": "回读校验通过：%d 字节完全一致" % len(plain2)}
                else:
                    verify = {"ok": False,
                              "detail": "回读校验不一致：文件 %d 字节 / 内存 %d 字节"
                                        % (len(plain2), len(self.plain))}
            except Exception as exc:
                verify = {"ok": False, "detail": "回读校验失败：%s" % exc}
            return {"ok": True, "backup": bak, "verify": verify,
                    "state": self.state()}

    # ---------------- 导出 ----------------
    def export(self, kind: str) -> dict:
        with self.lock:
            if self.plain is None:
                raise ApiError("还没有打开任何存档")
            base = self.path
            if kind == "plain":
                out = base + ".plain.bin"
                with open(out, "wb") as f:
                    f.write(self.plain)
            elif kind == "json":
                out = base + ".fields.json"
                rows = [{"path": e.path, "kind": e.kind, "key": e.key,
                         "value": e.value, "offset": e.offset, "size": e.size}
                        for e in self.entries() if e.kind != "key"]
                with open(out, "w", encoding="utf-8") as f:
                    json.dump(rows, f, ensure_ascii=False, indent=2)
            else:
                raise ApiError("未知的导出类型：%s" % kind)
            return {"ok": True, "path": out, "count": len(self.entries())}

    # ---------------- 自检 ----------------
    def selftest(self) -> dict:
        with self.lock:
            lines = [
                "Python  : %s" % sys.version.split()[0],
                "程序版本: %s" % core.APP_VERSION,
                "程序路径: %s" % os.path.abspath(core.__file__),
                "AES 可用: %s" % ("是" if core.AES is not None else "否（pip install pycryptodome）"),
            ]
            if self.plain is None:
                lines.append("")
                lines.append("尚未打开存档，跳过写入测试。")
                return {"ok": True, "lines": lines}
            lines.append("当前文件: %s" % self.path)
            lines.append("明文长度: %d 字节" % len(self.plain))
            ents = self.entries()
            nums = [e for e in ents if e.kind == "num"]
            lines.append("字段数量: %d 个数值 / %d 个字符串"
                         % (len(nums), sum(1 for e in ents if e.kind == "str")))
            if nums:
                t = nums[0]
                old = t.value
                try:
                    struct.pack_into("<q", self.plain, t.offset, old + 1)
                    after = [x.value for x in core.parse_entries(self.plain)
                             if x.kind == "num" and x.key == t.key]
                    struct.pack_into("<q", self.plain, t.offset, old)
                    lines.append("")
                    lines.append("写入测试对象: %s @ 0x%x 原值 %d" % (t.key, t.offset, old))
                    lines.append("写回后重新解析: %r" % (after,))
                    lines.append("结论: %s" % ("通过 —— 缓冲区可写、可解析"
                                              if (old + 1) in after
                                              else "失败 —— 写入未生效"))
                except Exception as exc:
                    lines.append("写入异常: %s: %s" % (type(exc).__name__, exc))
            return {"ok": True, "lines": lines}


SESSION = Session()


# --------------------------------------------------------------------------
# HTTP 层
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "aoe4-rlc/" + core.APP_VERSION
    protocol_version = "HTTP/1.1"

    # 关掉访问日志：本工具不需要任何日志输出
    def log_message(self, fmt, *args):
        pass

    def log_error(self, fmt, *args):
        pass

    # ---------- 工具 ----------
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _fail(self, msg: str, code: int = 400):
        self._json({"ok": False, "error": msg}, code)

    def _host_ok(self) -> bool:
        """只接受来自本机回环地址的请求（防 DNS rebinding）。"""
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        return host in ("127.0.0.1", "localhost", "[::1]", "::1")

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        if not raw:
            return {}
        try:
            obj = json.loads(raw.decode("utf-8"))
        except Exception:
            raise ApiError("请求体不是合法 JSON")
        if not isinstance(obj, dict):
            raise ApiError("请求体必须是 JSON 对象")
        return obj

    # ---------- 路由 ----------
    def do_GET(self):
        if not self._host_ok():
            return self._fail("拒绝非本机请求", 403)
        path = urlparse(self.path).path
        if path in STATIC_FILES:
            return self._static(*STATIC_FILES[path])
        try:
            if path == "/api/state":
                return self._json(SESSION.state())
            if path == "/api/candidates":
                return self._json({"ok": True, "items": core.list_candidates(),
                                   "accounts": core.list_accounts(),
                                   "users_dir": core.users_dir()})
            if path == "/api/selftest":
                return self._json(SESSION.selftest())
            return self._fail("未知接口：%s" % path, 404)
        except ApiError as exc:
            return self._fail(str(exc))
        except Exception as exc:
            return self._fail("%s: %s" % (type(exc).__name__, exc), 500)

    def do_POST(self):
        if not self._host_ok():
            return self._fail("拒绝非本机请求", 403)
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return self._fail("请用 application/json 提交", 415)
        path = urlparse(self.path).path
        try:
            body = self._body()
            if path == "/api/open":
                return self._json(SESSION.open(body.get("path", ""),
                                               body.get("account", "")))
            if path == "/api/edit":
                return self._json(SESSION.edit(body.get("index"), body.get("value")))
            if path == "/api/revert":
                return self._json(SESSION.revert(body.get("index")))
            if path == "/api/undo":
                return self._json(SESSION.undo_step())
            if path == "/api/save":
                return self._json(SESSION.save(body.get("account", "")))
            if path == "/api/close":
                return self._json(SESSION.close())
            if path == "/api/export":
                return self._json(SESSION.export(body.get("kind", "")))
            return self._fail("未知接口：%s" % path, 404)
        except ApiError as exc:
            return self._fail(str(exc))
        except Exception as exc:
            return self._fail("%s: %s" % (type(exc).__name__, exc), 500)

    def _static(self, name: str, ctype: str):
        full = os.path.join(WEB_DIR, name)
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError:
            return self._fail("前端资源缺失：%s" % full, 500)
        self._send(200, data, ctype)


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------
def _free_port(preferred: int) -> int:
    for port in [preferred] + list(range(preferred + 1, preferred + 20)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(port: int = 8765, open_browser: bool = True) -> int:
    port = _free_port(port)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = "http://127.0.0.1:%d/" % port
    print("AoE4 存档编辑器 后端已启动：%s" % url)
    print("提示：保存前请先完全退出游戏；如需修改生效，")
    print("      请在 Steam 启动选项中加入 -noCloudSaves（属性 -> 通用 -> 启动选项）。")
    print("按 Ctrl+C 结束服务。")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    port = int(os.environ.get("AOE4_RLC_PORT", "8765"))
    open_browser = True
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            print(__doc__)
            return 0
        if a in ("--port", "-p") and i + 1 < len(argv):
            port = int(argv[i + 1])
            i += 2
            continue
        if a == "--no-browser":
            open_browser = False
            i += 1
            continue
        print("未知参数：%s" % a)
        return 2
    return serve(port, open_browser)


if __name__ == "__main__":
    sys.exit(main())
