"""Read-only bridge to the pinned WeChat database reader."""

from __future__ import annotations

import atexit
import os
import sys
import tempfile
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PACKAGE_DIR = ROOT / "vendor" / "wechatauto"


def _load_reader():
    # Avoid the upstream package initializer, which imports GUI sending tools.
    package = types.ModuleType("wechatauto")
    package.__path__ = [str(PACKAGE_DIR)]
    sys.modules["wechatauto"] = package
    from wechatauto.param import WxParam
    WxParam.ENABLE_FILE_LOGGER = False
    from wechatauto.db import WeChatDB, auto_detect_db_dir
    return WeChatDB, auto_detect_db_dir


class WeChatReader:
    def __init__(self):
        self._scratch = tempfile.TemporaryDirectory(prefix="wechat_detector_")
        atexit.register(self.close)
        os.environ["WECHATAUTO_KEYS_DIR"] = str(Path(self._scratch.name) / "keys")
        WeChatDB, auto_detect_db_dir = _load_reader()
        db_dir = auto_detect_db_dir()
        if not db_dir:
            raise RuntimeError("未找到本机微信数据。请先登录 Windows 微信。")
        self.db = WeChatDB(
            db_dir=db_dir,
            workdir=str(Path(self._scratch.name) / "decrypted"),
        )

    def close(self):
        self._scratch.cleanup()

    def self_name(self):
        info = self.db.get_self_info()
        return info.get("remark") or info.get("nick_name") or ""

    def groups(self):
        sessions = self.db.get_sessions(limit=10000)
        group_sessions = [s for s in sessions
                          if str(s.get("username", "")).endswith("@chatroom")]
        ids = [s["username"] for s in group_sessions]
        names = {}
        contact_rel = next(
            rel for rel, path, _ in self.db._db_files
            if Path(path).name == "contact.db"
        )
        conn = self.db._open(contact_rel)
        try:
            for start in range(0, len(ids), 400):
                batch = ids[start:start + 400]
                if not batch:
                    continue
                rows = conn.execute(
                    "SELECT username,nick_name,remark FROM contact WHERE username IN ("
                    + ",".join("?" for _ in batch) + ")", batch,
                ).fetchall()
                names.update({r["username"]: r["remark"] or r["nick_name"] or ""
                              for r in rows})
        finally:
            conn.close()
        result = []
        for session in group_sessions:
            group_id = session["username"]
            latest_at = session.get("latest_at") or session.get("last_time") or 0
            try:
                latest_at = int(latest_at)
            except (TypeError, ValueError):
                latest_at = 0
            result.append({
                "id": group_id,
                "name": names.get(group_id) or "（未取得群名）",
                "latest_at": latest_at,
            })
        return result

    def read_initial(self, group_id):
        return list(reversed(self.db.get_messages(group_id, limit=50)))

    def read_new(self, group_id, after_seq):
        # Include the previous sequence again: WeChat can assign the same
        # sort_seq to more than one row. Store.save_messages deduplicates it.
        return self.db.get_new_messages(group_id, since_seq=max(0, after_seq - 1), limit=500)
