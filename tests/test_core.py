import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from model import analyze
from storage import Store


class StoreTests(unittest.TestCase):
    def test_cursor_and_task_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "tasks.db")
            store.upsert_groups([{"id": "g@chatroom", "name": "测试群"}])
            store.set_group_enabled("g@chatroom", True)
            messages = [
                {"sort_seq": 11, "local_id": 1, "sender_id": 3,
                 "create_time": 1700000000, "type": "文本", "content": "明天发报告"},
                {"sort_seq": 12, "local_id": 2, "sender_id": 2,
                 "create_time": 1700000001, "type": "文本", "content": "收到"},
            ]
            self.assertEqual(store.save_messages("g@chatroom", messages), 2)
            self.assertEqual(store.save_messages("g@chatroom", messages), 0)
            self.assertEqual(store.enabled_groups()[0]["last_seq"], 12)
            pending, _ = store.pending_batch("g@chatroom")
            store.apply_analysis("g@chatroom", pending, [
                {"title": "发报告", "due_at": None, "assignee": "me",
                 "confidence": 0.9, "source_ids": ["1"], "evidence": "明天发报告"}
            ])
            self.assertEqual(len(store.list_tasks()), 1)
            self.assertEqual(store.pending_batch("g@chatroom")[0], [])

    def test_bulk_task_status_update_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "tasks.db")
            store.upsert_groups([{"id": "g@chatroom", "name": "测试群"}])
            messages = [
                {"sort_seq": seq, "local_id": seq, "create_time": 1700000000 + seq,
                 "type": "文本", "content": f"请处理事项 {seq}"}
                for seq in (1, 2)
            ]
            store.save_messages("g@chatroom", messages)
            pending, _ = store.pending_batch("g@chatroom")
            store.apply_analysis("g@chatroom", pending, [
                {"title": f"事项 {seq}", "assignee": "me", "confidence": 0.9,
                 "source_ids": [str(seq)], "evidence": f"请处理事项 {seq}"}
                for seq in (1, 2)
            ])
            task_ids = [task["id"] for task in store.list_tasks()]
            self.assertEqual(store.update_tasks_status(task_ids, "已完成"), 2)
            self.assertTrue(all(task["status"] == "已完成" for task in store.list_tasks()))

            with self.assertRaisesRegex(ValueError, "部分事项已不存在"):
                store.update_tasks_status([task_ids[0], 999], "忽略")
            self.assertTrue(all(task["status"] == "已完成" for task in store.list_tasks()))

    def test_groups_are_sorted_by_latest_message_time(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "tasks.db")
            store.upsert_groups([
                {"id": "old@chatroom", "name": "旧群", "latest_at": 10},
                {"id": "new@chatroom", "name": "新群", "latest_at": 20},
            ])
            self.assertEqual([g["id"] for g in store.list_groups()],
                             ["new@chatroom", "old@chatroom"])


class ModelTests(unittest.TestCase):
    @patch("model.requests.post")
    def test_all_messages_reach_deepseek(self, post):
        response = Mock(ok=True)
        response.json.return_value = {
            "choices": [{"message": {"content": '{"tasks":[]}'}}]
        }
        post.return_value = response
        messages = [
            {"sent_at": 1700000000, "sender": "别人", "type": "文本", "content": "闲聊"},
            {"sent_at": 1700000001, "sender": "别人", "type": "文本", "content": "请发报告"},
        ]
        self.assertEqual(analyze("群", messages, [], ["我"], "test-key"), [])
        sent = post.call_args.kwargs["json"]
        self.assertEqual(sent["model"], "deepseek-flash")
        self.assertIn("闲聊", sent["messages"][1]["content"])
        self.assertIn("请发报告", sent["messages"][1]["content"])


if __name__ == "__main__":
    unittest.main()
