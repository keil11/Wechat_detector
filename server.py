"""Local web client and background WeChat monitor."""

from __future__ import annotations

import secrets
import os
import threading
import webbrowser
from datetime import datetime
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request

from model import analyze, load_api_key, save_api_key
from reader import WeChatReader
from storage import Store


ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = 8765
app = Flask(__name__)
store = Store(ROOT / "data" / "tasks.sqlite3")
csrf_token = secrets.token_urlsafe(32)


class Monitor:
    def __init__(self):
        self.reader = None
        self.status = "正在连接本机微信…"
        self.running = False
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.thread = None
        threading.Thread(target=self._bootstrap, daemon=True).start()

    def _set_status(self, value):
        with self.lock:
            self.status = value

    def snapshot(self):
        with self.lock:
            return self.status, self.running, self.reader is not None

    def _bootstrap(self):
        try:
            reader = WeChatReader()
            groups = reader.groups()
            store.upsert_groups(groups)
            if not store.get_setting("my_names"):
                name = reader.self_name()
                if name:
                    store.set_setting("my_names", name)
            with self.lock:
                self.reader = reader
                self.status = f"已连接本机微信，找到 {len(groups)} 个群聊"
        except Exception as exc:
            self._set_status("读取本机微信失败：" + str(exc))

    def start(self):
        with self.lock:
            if self.running:
                return None
            if self.reader is None:
                return "本机微信尚未连接，请稍候刷新页面"
            if not store.enabled_groups():
                return "请先选择要监听的群"
            if not load_api_key():
                return "请先保存 DeepSeek API 密钥"
            self.stop_event.clear()
            self.running = True
            self.status = "正在启动监听…"
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()
        return None

    def stop(self):
        self.stop_event.set()
        self._set_status("正在停止；当前模型请求结束后暂停")

    def _loop(self):
        try:
            while not self.stop_event.is_set():
                groups = store.enabled_groups()
                if not groups:
                    self._set_status("没有已选群聊；请至少选择一个群")
                    if self.stop_event.wait(10):
                        break
                    continue
                for group in groups:
                    if self.stop_event.is_set():
                        break
                    group_id = group["id"]
                    cursor = group["last_seq"]
                    self._set_status(f"正在检查「{group['name']}」的新消息…")
                    inserted_count = 0
                    if cursor is None:
                        # First run for this group: analyze the 50 newest local messages.
                        initial = self.reader.read_initial(group_id)
                        inserted_count += store.save_messages(group_id, initial)
                    else:
                        while not self.stop_event.is_set():
                            new = self.reader.read_new(group_id, cursor)
                            if not new:
                                break
                            inserted = store.save_messages(group_id, new)
                            inserted_count += inserted
                            next_cursor = max(int(m["sort_seq"]) for m in new)
                            if next_cursor <= cursor and not inserted:
                                break
                            cursor = max(cursor, next_cursor)
                            if len(new) < 500:
                                break
                    self._set_status(
                        f"已检查「{group['name']}」；新增 {inserted_count} 条消息"
                    )
                    # Every message goes to the model. No keyword or @ prefilter.
                    for _ in range(3):
                        if self.stop_event.is_set():
                            break
                        pending, context = store.pending_batch(group_id, limit=20)
                        if not pending:
                            break
                        names = [name.strip() for name in
                                 store.get_setting("my_names").replace("，", ",").split(",")
                                 if name.strip()]
                        self._set_status(f"正在分析「{group['name']}」的 {len(pending)} 条消息…")
                        tasks = analyze(group["name"], pending, context, names, load_api_key())
                        store.apply_analysis(group_id, pending, tasks)
                        self._set_status(f"已分析「{group['name']}」；模型提出 {len(tasks)} 条候选待办")
                if not self.stop_event.is_set():
                    self._set_status(f"监听中，已检查 {len(groups)} 个群")
                if self.stop_event.wait(10):
                    break
        except Exception as exc:
            self._set_status("监听出错并已暂停：" + str(exc))
        else:
            self._set_status("监听已停止")
        finally:
            with self.lock:
                self.running = False


monitor = Monitor()


@app.before_request
def protect_local_api():
    if request.host not in (f"{HOST}:{PORT}", f"localhost:{PORT}"):
        abort(403)
    if request.method == "POST" and request.headers.get("X-CSRF-Token") != csrf_token:
        abort(403)


@app.get("/")
def index():
    return render_template("index.html", csrf_token=csrf_token)


@app.get("/api/state")
def state():
    status, running, ready = monitor.snapshot()
    return jsonify({
        "groups": store.list_groups(),
        "tasks": store.list_tasks(),
        "status": status,
        "running": running,
        "ready": ready,
        "key_configured": bool(load_api_key()),
        "my_names": store.get_setting("my_names"),
    })


@app.post("/api/groups/<path:group_id>")
def set_group(group_id):
    data = request.get_json(silent=True) or {}
    if not any(g["id"] == group_id for g in store.list_groups()):
        abort(404)
    store.set_group_enabled(group_id, bool(data.get("enabled")))
    return jsonify({"ok": True})


@app.post("/api/settings")
def settings():
    data = request.get_json(silent=True) or {}
    store.set_setting("my_names", str(data.get("my_names") or "").strip()[:200])
    return jsonify({"ok": True})


@app.post("/api/key")
def key():
    data = request.get_json(silent=True) or {}
    value = str(data.get("key") or "").strip()
    if not value:
        return jsonify({"error": "请输入 API 密钥"}), 400
    save_api_key(value)
    return jsonify({"ok": True})


@app.post("/api/monitor/start")
def start():
    error = monitor.start()
    if error:
        return jsonify({"error": error}), 400
    return jsonify({"ok": True})


@app.post("/api/monitor/stop")
def stop():
    monitor.stop()
    return jsonify({"ok": True})


@app.post("/api/tasks/<int:task_id>")
def update_task(task_id):
    data = request.get_json(silent=True) or {}
    if not any(t["id"] == task_id for t in store.list_tasks()):
        abort(404)
    status = data.get("status")
    title = data.get("title")
    due = data.get("due_at")
    if title is not None and not str(title).strip():
        return jsonify({"error": "事项不能为空"}), 400
    if due:
        try:
            datetime.fromisoformat(str(due))
        except ValueError:
            return jsonify({"error": "截止时间格式有误"}), 400
    try:
        store.update_task(task_id, title=title, due_at=due, status=status)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True})


@app.post("/api/tasks/bulk")
def update_tasks_bulk():
    data = request.get_json(silent=True) or {}
    task_ids = data.get("task_ids")
    if not isinstance(task_ids, list) or not task_ids or len(task_ids) > 500:
        return jsonify({"error": "请选择 1 到 500 个事项"}), 400
    try:
        updated = store.update_tasks_status(task_ids, data.get("status"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, "updated": updated})


if __name__ == "__main__":
    if os.environ.get("WECHAT_DETECTOR_NO_BROWSER") != "1":
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{HOST}:{PORT}")).start()
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False, threaded=True)
