# -*- coding: utf-8 -*-
"""本地看板服务：http://127.0.0.1:8765"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

import config

# 演示模式必须在导入 single / store 之前切换数据目录（它们在导入时绑定路径）
if "--demo" in sys.argv:
    config.use_demo_dir()

import ai
import datasource as ds
import single
import store

app = Flask(__name__, static_folder=str(config.WEB_DIR), static_url_path="")
app.config["JSON_AS_ASCII"] = False
LOCK = config.DATA_DIR / ".run.lock"


def is_running():
    if not LOCK.exists():
        return False
    try:
        return time.time() - LOCK.stat().st_mtime < 900
    except OSError:
        return False


@app.route("/")
def index():
    return send_from_directory(str(config.WEB_DIR), "index.html")


@app.route("/api/latest")
def api_latest():
    if config.LATEST_PATH.exists():
        try:
            return app.response_class(
                config.LATEST_PATH.read_text(encoding="utf-8"),
                mimetype="application/json",
            )
        except Exception:
            pass
    data = store.latest()
    if data:
        return jsonify(data)
    return jsonify({"empty": True, "message": "尚未生成报告"})


@app.route("/api/search")
def api_search():
    try:
        return jsonify({"ok": True, "items": single.search(request.args.get("q", ""))})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)[:160], "items": []}), 500


DEMO_NOTICE = "演示模式：该功能需要实时抓取行情，已关闭。想跑真实数据请在本地执行 python pipeline.py --force"


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    if config.DEMO_MODE:
        return jsonify({"ok": False, "demo": True, "error": DEMO_NOTICE}), 403
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(single.analyze(data.get("code"), data.get("buy_price")))
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)[:160]}), 400


@app.route("/api/comment", methods=["POST"])
def api_comment():
    if config.DEMO_MODE:
        return jsonify({"ok": False, "demo": True, "error": DEMO_NOTICE}), 403
    data = request.get_json(silent=True) or {}
    try:
        result = single.analyze(data.get("code"), data.get("buy_price"))
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)[:160]}), 400
    return jsonify(ai.comment_stock(result))


@app.route("/api/history")
def api_history():
    return jsonify({"items": store.history(int(request.args.get("limit", 12)))})


@app.route("/api/status")
def api_status():
    latest = store.latest() or {}
    return jsonify({
        "running": is_running(),
        "generated_at": latest.get("generated_at"),
        "session": ds.session_state(),
        "model": config.MODEL,
        "port": config.PORT,
        "demo": config.DEMO_MODE,
    })


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    if config.DEMO_MODE:
        return jsonify({"ok": False, "demo": True, "message": DEMO_NOTICE}), 403
    if is_running():
        return jsonify({"ok": False, "message": "已有任务在运行"}), 429
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    p = subprocess.Popen(
        [sys.executable, str(config.BASE_DIR / "pipeline.py"), "--force"],
        cwd=str(config.BASE_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    return jsonify({"ok": True, "pid": p.pid, "message": "已开始分析，约 1 分钟"})


@app.route("/api/health")
def api_health():
    return jsonify({"ok": True, "time": datetime.now().isoformat(timespec="seconds")})


def main():
    config.setup_stdout()
    if config.DEMO_MODE:
        print("演示模式（预置快照，不联网）数据目录: %s" % config.DATA_DIR, flush=True)
    print("A股短线雷达: http://%s:%d" % (config.HOST, config.PORT), flush=True)
    app.run(host=config.HOST, port=config.PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
