# -*- coding: utf-8 -*-
"""
公歧导航 <-> 飞书表格 双向同步服务
----------------------------------
作用：在本机运行一个轻量 HTTP 服务，负责在「导航页(HTML, localStorage)」与
「飞书电子表格」之间来回同步数据。

依赖：仅 Python 标准库（http.server / urllib），无需 pip 安装第三方包。

启动：  python sync_server.py
默认监听： http://127.0.0.1:8787

API：
  GET  /api/health                    健康检查
  GET  /api/pull                      从飞书表格读取导航数据（返回 JSON: {bigCategories:[...]})
  POST /api/push                      把网页导航数据写入飞书表格（body: {bigCategories:[...]})
  GET  /api/config                    返回当前配置（含飞书表格链接，不含密钥）

网页侧集成：
  - 网页每次保存(编辑/增删/拖拽)后调用  POST /api/push  推送最新数据
  - 网页定时(如每30秒)或启动时调用  GET /api/pull    拉取飞书表格最新数据

说明：
  - 首次 push 时若尚未创建飞书表格，会用应用凭证自动创建一张表格并持久化到 _config.json。
  - 配置项 APP_ID / APP_SECRET 为飞书开放平台自建应用凭证，请按实际填写。
"""

import json
import os
import time
import traceback
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ============ 配置区（按需修改） ============
# 【重要】部署到公开仓库时，请将凭证留空，避免泄露。
# 使用前请填写你在飞书开放平台创建的自建应用凭证：
#   APP_ID     = 你的应用 App ID（如 cli_xxxxxxxx）
#   APP_SECRET = 你的应用 App Secret（如 abcdef123456）
# 需为应用开通权限：sheets:spreadsheet、sheets:spreadsheet:create、
#   sheets:spreadsheet:read、sheets:spreadsheet:readonly、drive:drive
# 首次运行会自动用该凭证创建飞书表格；也可在 _config.json 中直接指定已有表格。
APP_ID     = ""
APP_SECRET = ""
PORT = 8787
# ============================================

BASE = "https://open.feishu.cn/open-apis"
_CFG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_config.json")

# 全局配置（运行期加载/保存）
CFG = {"spreadsheet_token": "", "sheet_id": "", "spreadsheet_url": ""}


def _log_err(tag, e):
    try:
        with open(os.path.join(os.path.dirname(__file__), "_error.log"), "a", encoding="utf-8") as f:
            f.write("\n[%s] %s: %s\n%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), tag, e, traceback.format_exc()))
    except Exception:
        pass


def _load_config():
    global CFG
    try:
        if os.path.exists(_CFG_PATH):
            with open(_CFG_PATH, "r", encoding="utf-8") as f:
                CFG.update(json.load(f))
    except Exception as e:
        _log_err("load_config", e)


def _save_config():
    try:
        with open(_CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(CFG, f, ensure_ascii=False, indent=2)
    except Exception as e:
        _log_err("save_config", e)


def get_token():
    body = json.dumps({"app_id": APP_ID, "app_secret": APP_SECRET}).encode("utf-8")
    req = urllib.request.Request(
        BASE + "/auth/v3/tenant_access_token/internal",
        data=body, headers={"Content-Type": "application/json; charset=utf-8"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        r = json.loads(resp.read().decode("utf-8"))
    if r.get("code") != 0:
        raise RuntimeError("获取token失败: %s %s" % (r.get("code"), r.get("msg")))
    return r["tenant_access_token"]


def _req(method, url, token, payload=None):
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json; charset=utf-8"}
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"code": e.code, "msg": str(e)}


def ensure_sheet(token):
    """确保已创建飞书表格。若未创建，则自动创建 spreadsheet 与子表。"""
    if CFG.get("spreadsheet_token") and CFG.get("sheet_id"):
        return
    # 创建 spreadsheet
    r = _req("POST", BASE + "/sheets/v3/spreadsheets", token, {"title": "公歧导航-数据同步"})
    if r.get("code") != 0:
        raise RuntimeError("创建飞书表格失败: %s %s" % (r.get("code"), r.get("msg")))
    st = r["data"]["spreadsheet"]["spreadsheet_token"]
    url = r["data"]["spreadsheet"]["url"]
    # 获取默认 sheet（新建表自带一个 sheet）
    sheet_id = None
    mr = _req("GET", "%s/sheets/v3/spreadsheets/%s/sheets/query" % (BASE, st), token)
    if mr.get("code") == 0 and mr.get("data", {}).get("sheets"):
        sheet_id = mr["data"]["sheets"][0]["sheet_id"]
    if not sheet_id:
        sheet_id = "sheet1"  # 兜底
    # 写入表头
    hdr = {"valueRange": {"range": "%s!A1:E1" % sheet_id, "values": [["大分类", "子分类", "网址名称", "网址URL", "Logo"]]}}
    _req("PUT", "%s/sheets/v2/spreadsheets/%s/values" % (BASE, st), token, hdr)
    CFG.update({"spreadsheet_token": st, "sheet_id": sheet_id, "spreadsheet_url": url})
    _save_config()


def read_sheet(token):
    if not (CFG.get("spreadsheet_token") and CFG.get("sheet_id")):
        return [["大分类", "子分类", "网址名称", "网址URL", "Logo"]]
    st = CFG["spreadsheet_token"]; sh = CFG["sheet_id"]
    url = "%s/sheets/v2/spreadsheets/%s/values/%s!A1:E200000" % (BASE, st, sh)
    r = _req("GET", url, token)
    if r.get("code") != 0:
        raise RuntimeError("读取飞书表失败: %s %s" % (r.get("code"), r.get("msg")))
    return r.get("data", {}).get("valueRange", {}).get("values", [])


def write_sheet(token, rows):
    ensure_sheet(token)
    st = CFG["spreadsheet_token"]; sh = CFG["sheet_id"]
    # 清空旧数据（保留表头）
    clear_url = "%s/sheets/v2/spreadsheets/%s/values_range_batch_update" % (BASE, st)
    _req("POST", clear_url, token, {"ranges": [{"range": "%s!A2:E200000" % sh}]})
    # 分批写入（飞书单次写入有行数限制，每批 300 行）
    BATCH = 300
    for start in range(0, len(rows), BATCH):
        chunk = rows[start:start + BATCH]
        value_range = {"valueRange": {"range": "%s!A%d:E%d" % (sh, start + 1, start + len(chunk)), "values": chunk}}
        r = _req("PUT", "%s/sheets/v2/spreadsheets/%s/values" % (BASE, st), token, value_range)
        if r.get("code") != 0:
            raise RuntimeError("写入飞书表失败: %s %s" % (r.get("code"), r.get("msg")))
    return len(rows)


# ---------- 数据结构转换 ----------
def nav_to_rows(bigCategories):
    rows = [["大分类", "子分类", "网址名称", "网址URL", "Logo"]]
    for big in bigCategories or []:
        bname = big.get("name", "")
        for sub in big.get("children") or []:
            sname = sub.get("name", "")
            sites = sub.get("sites") or []
            if not sites:
                rows.append([bname, sname, "", "", ""])
                continue
            for s in sites:
                rows.append([bname, sname, s.get("name", ""), s.get("url", ""), s.get("logo", "") or ""])
    return rows


def rows_to_nav(rows):
    big_map = {}
    order_big = []
    for row in rows[1:]:
        if len(row) < 5:
            row = (row + [""] * 5)[:5]
        bname, sname, name, url, logo = row[0], row[1], row[2], row[3], row[4]
        if not bname:
            continue
        if bname not in big_map:
            big_map[bname] = {"id": "b" + str(len(order_big) + 1), "name": bname, "children": []}
            order_big.append(bname)
        big = big_map[bname]
        sub = None
        for c in big["children"]:
            if c["name"] == sname:
                sub = c
                break
        if sub is None:
            sub = {"id": "s" + str(len(order_big)) + "-" + str(len(big["children"]) + 1), "name": sname or "未命名", "sites": []}
            big["children"].append(sub)
        if name:
            sub["sites"].append({
                "id": "w" + str(len(order_big)) + "-" + str(len(sub["sites"]) + 1),
                "name": name, "url": url, "logo": logo,
            })
    return [big_map[b] for b in order_big]


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        try:
            if self.path.startswith("/api/health"):
                url = CFG.get("spreadsheet_url") or ""
                return self._json({"ok": True, "service": "gongqi-sync", "spreadsheet_url": url})
            if self.path.startswith("/api/pull"):
                token = get_token()
                rows = read_sheet(token)
                nav = rows_to_nav(rows)
                return self._json({"ok": True, "bigCategories": nav, "rows": len(rows) - 1})
            if self.path.startswith("/api/config"):
                return self._json({"ok": True, "spreadsheet_url": CFG.get("spreadsheet_url") or "", "token": bool(CFG.get("spreadsheet_token"))})
            return self._json({"ok": False, "msg": "unknown endpoint"}, 404)
        except Exception as e:
            _log_err("GET", e)
            return self._json({"ok": False, "msg": str(e)}, 500)

    def do_POST(self):
        try:
            if self.path.startswith("/api/push"):
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length).decode("utf-8")
                payload = json.loads(raw)
                if "bigCategories" not in payload:
                    return self._json({"ok": False, "msg": "缺少 bigCategories"}, 400)
                token = get_token()
                rows = nav_to_rows(payload["bigCategories"])
                n = write_sheet(token, rows)
                return self._json({"ok": True, "rows_written": n, "spreadsheet_url": CFG.get("spreadsheet_url")})
            return self._json({"ok": False, "msg": "unknown endpoint"}, 404)
        except Exception as e:
            _log_err("POST", e)
            return self._json({"ok": False, "msg": str(e)}, 500)

    def log_message(self, fmt, *args):
        print("[%s] %s" % (time.strftime("%H:%M:%S"), fmt % args))


if __name__ == "__main__":
    _load_config()
    print("=" * 56)
    print("公歧导航 <-> 飞书表格 双向同步服务")
    if CFG.get("spreadsheet_url"):
        print("飞书表格: " + CFG["spreadsheet_url"])
    else:
        print("飞书表格: (首次推送数据时自动创建)")
    print("监听地址: http://127.0.0.1:%d" % PORT)
    print("请保持本窗口运行。按 Ctrl+C 停止。")
    print("=" * 56)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
