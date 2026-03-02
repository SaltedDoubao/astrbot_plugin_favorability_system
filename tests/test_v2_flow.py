import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import threading
import types
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _install_astrbot_stubs(data_path: str | None = None):
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event_mod = types.ModuleType("astrbot.api.event")
    star_mod = types.ModuleType("astrbot.api.star")

    class _Logger:
        def info(self, *_args, **_kwargs):
            pass

        def warning(self, *_args, **_kwargs):
            pass

        def error(self, *_args, **_kwargs):
            pass

    def _identity_decorator(*_args, **_kwargs):
        def inner(obj):
            return obj

        return inner

    class _Filter:
        command = staticmethod(_identity_decorator)
        on_llm_request = staticmethod(_identity_decorator)
        on_llm_response = staticmethod(_identity_decorator)
        after_message_sent = staticmethod(_identity_decorator)

    class _Star:
        def __init__(self, _context):
            pass

    class _Context:
        pass

    class _AstrMessageEvent:
        pass

    api.llm_tool = _identity_decorator
    api.logger = _Logger()
    event_mod.filter = _Filter()
    event_mod.AstrMessageEvent = _AstrMessageEvent
    star_mod.Context = _Context
    star_mod.Star = _Star
    star_mod.register = _identity_decorator

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.star"] = star_mod

    if data_path is not None:
        core_mod = types.ModuleType("astrbot.core")
        utils_mod = types.ModuleType("astrbot.core.utils")
        path_mod = types.ModuleType("astrbot.core.utils.astrbot_path")

        def _get_astrbot_data_path():
            return data_path

        path_mod.get_astrbot_data_path = _get_astrbot_data_path
        sys.modules["astrbot.core"] = core_mod
        sys.modules["astrbot.core.utils"] = utils_mod
        sys.modules["astrbot.core.utils.astrbot_path"] = path_mod
    else:
        sys.modules.pop("astrbot.core", None)
        sys.modules.pop("astrbot.core.utils", None)
        sys.modules.pop("astrbot.core.utils.astrbot_path", None)


def _load_plugin_module(data_path: str | None = None):
    _install_astrbot_stubs(data_path=data_path)
    package_name = "favorability_testpkg"
    package = types.ModuleType(package_name)
    package.__path__ = [str(ROOT)]
    sys.modules[package_name] = package

    db_spec = importlib.util.spec_from_file_location(
        f"{package_name}.db", ROOT / "db.py"
    )
    db_mod = importlib.util.module_from_spec(db_spec)
    assert db_spec and db_spec.loader
    db_spec.loader.exec_module(db_mod)
    sys.modules[f"{package_name}.db"] = db_mod

    main_spec = importlib.util.spec_from_file_location(
        f"{package_name}.main", ROOT / "main.py"
    )
    main_mod = importlib.util.module_from_spec(main_spec)
    assert main_spec and main_spec.loader
    main_spec.loader.exec_module(main_mod)
    sys.modules[f"{package_name}.main"] = main_mod
    return main_mod, db_mod


class _Req:
    def __init__(self):
        self.system_prompt = ""


class _Resp:
    def __init__(self, completion_text: str):
        self.completion_text = completion_text


class _MessageObj:
    def __init__(self, message_id: str):
        self.message_id = message_id


class _FakeEvent:
    def __init__(
        self,
        *,
        sender_id: str = "u1",
        sender_name: str = "用户",
        group_id: str = "100",
        message_str: str = "",
        message_id: str = "m1",
        private_chat: bool = False,
        is_admin: bool = False,
        role: str = "",
    ):
        self._sender_id = sender_id
        self._sender_name = sender_name
        self._group_id = group_id
        self._private_chat = private_chat
        self._is_admin = is_admin
        self.role = role
        self.message_str = message_str
        self.message_obj = _MessageObj(message_id)
        self.unified_msg_origin = "origin"

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def is_private_chat(self):
        return self._private_chat

    def is_admin(self):
        return self._is_admin

    def get_group_id(self):
        return self._group_id

    def plain_result(self, text: str):
        return text


class FavorabilityV2FlowTests(unittest.TestCase):
    def setUp(self):
        self.main_mod, self.db_mod = _load_plugin_module()
        self.plugin = self.main_mod.FavorabilityPlugin(None, {})
        self.plugin.min_level = -100
        self.plugin.max_level = 100
        self.plugin.initial_level = 0
        self.plugin.tiers = [
            {"name": "敌对", "min": -100, "max": -51, "effect": "敌对"},
            {"name": "冷淡", "min": -50, "max": -11, "effect": "冷淡"},
            {"name": "中立", "min": -10, "max": 9, "effect": "中立"},
            {"name": "友好", "min": 10, "max": 39, "effect": "友好"},
            {"name": "亲密", "min": 40, "max": 100, "effect": "亲密"},
        ]
        self.td = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.td.name, "fav.db")
        self.plugin.db = self.db_mod.FavorabilityDB(db_path)
        self.plugin.data_dir = self.td.name

    def tearDown(self):
        if self.plugin.db:
            self.plugin.db.close()
        self.td.cleanup()

    async def _collect(self, agen):
        items = []
        async for item in agen:
            items.append(item)
        return items

    def test_removed_tools_from_source(self):
        content = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertNotIn('@llm_tool(name="fav_profile")', content)
        self.assertNotIn('@llm_tool(name="fav_assess")', content)

    def test_classifier_priority_and_conservative_negative(self):
        cls = self.plugin._classify_interaction_rule_v1("谢谢你，讲得很详细")
        self.assertIsNotNone(cls)
        self.assertEqual(cls["interaction_type"], "thanks")
        self.assertEqual(cls["evidence"], "KW_THANKS")

        abuse = self.plugin._classify_interaction_rule_v1("你真是傻逼")
        self.assertEqual(abuse["interaction_type"], "abuse")

        conservative_cold = self.plugin._classify_interaction_rule_v1("好冷淡，行吧")
        self.assertIsNone(conservative_cold)

    def test_apply_assessment_internal_result_shape(self):
        self.plugin.db.add_user("group", "100", "u1", 0)
        ok, result = self.plugin._apply_assessment_internal(
            session_type="group",
            session_id="100",
            user_id="u1",
            interaction_type="helpful_dialogue",
            intensity=1,
            evidence="KW_HELPFUL",
            source="auto_hook",
        )
        self.assertTrue(ok)
        assert isinstance(result, dict)
        self.assertIn("old_level", result)
        self.assertIn("new_level", result)
        self.assertIn("final_delta", result)
        self.assertIn("factors", result)

        cnt = self.plugin.db.count_positive_events_by_type_since(
            "group", "100", "u1", "helpful_dialogue", 0
        )
        self.assertEqual(cnt, 1)

    def test_apply_assessment_internal_is_transactional_under_concurrency(self):
        self.plugin.db.add_user("group", "100", "u1", 0)

        def _worker():
            ok, _ = self.plugin._apply_assessment_internal(
                session_type="group",
                session_id="100",
                user_id="u1",
                interaction_type="helpful_dialogue",
                intensity=1,
                evidence="KW_HELPFUL",
                source="auto_hook",
            )
            self.assertTrue(ok)

        t1 = threading.Thread(target=_worker)
        t2 = threading.Thread(target=_worker)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        user = self.plugin.db.get_user("group", "100", "u1")
        self.assertIsNotNone(user)
        assert user is not None
        # 第一次 +5，第二次因 anti-spam 变为 +4，总计 +9。
        self.assertEqual(user.level, 9)
        cnt = self.plugin.db.count_positive_events_by_type_since(
            "group", "100", "u1", "helpful_dialogue", 0
        )
        self.assertEqual(cnt, 2)

    def test_hooks_inject_and_assess_without_tools(self):
        event = _FakeEvent(message_str="谢谢你")
        req = _Req()
        resp = _Resp("不客气")

        asyncio.run(self.plugin.on_llm_request(event, req))
        self.assertIn("当前用户交互风格", req.system_prompt)

        asyncio.run(self.plugin.on_llm_response(event, resp))
        asyncio.run(self.plugin.after_message_sent(event))

        cnt = self.plugin.db.count_positive_events_by_type_since(
            "group", "100", "u1", "thanks", 0
        )
        self.assertEqual(cnt, 1)

    def test_style_prompt_uses_effect_not_tier_name(self):
        self.plugin.tiers = [
            {"name": "A", "min": -100, "max": -1, "effect": "保持距离"},
            {"name": "B", "min": 0, "max": 100, "effect": "温和主动"},
        ]
        event = _FakeEvent(sender_id="u-style", group_id="g-style")
        self.plugin.db.add_user("group", "g-style", "u-style", 10)
        req = _Req()
        asyncio.run(self.plugin.on_llm_request(event, req))
        self.assertIn("温和主动", req.system_prompt)

    def test_tier_change_notice_injected_once(self):
        self.plugin.db.add_user("group", "100", "u2", 9)
        ok, result = self.plugin._apply_assessment_internal(
            session_type="group",
            session_id="100",
            user_id="u2",
            interaction_type="thanks",
            intensity=1,
            evidence="KW_THANKS",
            source="auto_hook",
        )
        self.assertTrue(ok)
        assert isinstance(result, dict)
        self.assertTrue(result["tier_changed"])

        event = _FakeEvent(sender_id="u2", group_id="100", message_id="tier-msg")
        req1 = _Req()
        asyncio.run(self.plugin.on_llm_request(event, req1))
        self.assertIn("状态变化提示", req1.system_prompt)

        req2 = _Req()
        asyncio.run(self.plugin.on_llm_request(event, req2))
        self.assertNotIn("状态变化提示", req2.system_prompt)

    def test_management_tools_require_admin(self):
        self.plugin.db.add_user("group", "100", "u1", 0)
        non_admin_event = _FakeEvent(sender_id="u-op", group_id="100", is_admin=False)
        denied = asyncio.run(self.plugin.fav_update(non_admin_event, "u1", 10))
        self.assertIn("权限不足", denied)

        admin_event = _FakeEvent(sender_id="u-op", group_id="100", is_admin=True)
        ok_msg = asyncio.run(self.plugin.fav_update(admin_event, "u1", 10))
        self.assertIn("好感度更新为 10", ok_msg)

    def test_fav_query_shows_daily_cap_and_boundary_hint(self):
        today = self.plugin._get_today_bucket()
        self.plugin.db.add_user(
            "group",
            "100",
            "u-boundary",
            100,
            daily_pos_gain=50,
            daily_neg_gain=0,
            daily_bucket=today,
        )
        event = _FakeEvent(sender_id="u-boundary", group_id="100")
        results = asyncio.run(self._collect(self.plugin.cmd_fav_query(event)))
        text = "\n".join(results)
        self.assertIn("今日正向增益: 50/50", text)
        self.assertIn("已达上限", text)
        self.assertIn("触达边界", text)

    def test_fav_reset_and_fav_reset_all(self):
        today = self.plugin._get_today_bucket()
        self.plugin.db.add_user(
            "group",
            "100",
            "u-self",
            66,
            daily_pos_gain=10,
            daily_neg_gain=3,
            daily_bucket=today,
        )
        self.plugin.initial_level = 12
        self_event = _FakeEvent(sender_id="u-self", group_id="100", message_str="fav-reset")
        reset_messages = asyncio.run(self._collect(self.plugin.cmd_fav_reset(self_event)))
        self.assertTrue(any("重置为 12" in msg for msg in reset_messages))
        user = self.plugin.db.get_user("group", "100", "u-self")
        assert user is not None
        self.assertEqual(user.level, 12)
        self.assertEqual(user.daily_pos_gain, 0)
        self.assertEqual(user.daily_neg_gain, 0)

        self.plugin.db.add_user("group", "100", "u-a", 1)
        self.plugin.db.add_user("group", "100", "u-b", 2)
        admin_event = _FakeEvent(
            sender_id="u-admin", group_id="100", message_str="fav-reset-all", is_admin=True
        )
        all_messages = asyncio.run(self._collect(self.plugin.cmd_fav_reset_all(admin_event)))
        self.assertTrue(any("已重置当前会话" in msg for msg in all_messages))
        u_a = self.plugin.db.get_user("group", "100", "u-a")
        u_b = self.plugin.db.get_user("group", "100", "u-b")
        assert u_a is not None and u_b is not None
        self.assertEqual(u_a.level, 12)
        self.assertEqual(u_b.level, 12)

    def test_fav_export_json_and_csv_files(self):
        self.plugin.db.add_user("group", "100", "u1", 10)
        self.plugin.db.upsert_current_nickname("group", "100", "u1", "用户1")
        self.plugin.db.log_score_event(
            "group",
            "100",
            "u1",
            "thanks",
            1,
            4,
            4,
            1.0,
            int(1730000000),
            "KW_THANKS",
        )
        admin_event = _FakeEvent(sender_id="u-admin", group_id="100", is_admin=True)
        json_msg = asyncio.run(self.plugin.fav_export(admin_event, format="json", scope="session"))
        self.assertIn("导出成功（JSON）", json_msg)
        json_path = json_msg.split("文件: ", 1)[1].strip()
        self.assertTrue(os.path.exists(json_path))

        csv_msg = asyncio.run(self.plugin.fav_export(admin_event, format="csv", scope="global"))
        self.assertIn("导出成功（CSV ZIP）", csv_msg)
        zip_path = csv_msg.split("文件: ", 1)[1].strip()
        self.assertTrue(os.path.exists(zip_path))
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = set(zf.namelist())
        self.assertTrue({"users.csv", "nicknames.csv", "score_events.csv"}.issubset(names))

    def test_fav_stats_session_and_global(self):
        self.plugin.db.add_user("group", "100", "u1", 10, daily_pos_gain=5, daily_neg_gain=0)
        self.plugin.db.add_user("group", "200", "u2", 20, daily_pos_gain=8, daily_neg_gain=2)
        self.plugin.db.log_score_event(
            "group",
            "100",
            "u1",
            "thanks",
            1,
            4,
            4,
            1.0,
            int(1730000000),
            "KW_THANKS",
        )
        self.plugin.db.log_score_event(
            "group",
            "200",
            "u2",
            "thanks",
            1,
            4,
            4,
            1.0,
            int(1730000010),
            "KW_THANKS",
        )

        admin_event = _FakeEvent(sender_id="u-admin", group_id="100", is_admin=True)
        session_msg = asyncio.run(self.plugin.fav_stats(admin_event, scope="session"))
        global_msg = asyncio.run(self.plugin.fav_stats(admin_event, scope="global"))
        self.assertIn("统计范围: 会话 group:100", session_msg)
        self.assertIn("用户数: 1", session_msg)
        self.assertIn("统计范围: 全局", global_msg)
        self.assertIn("用户数: 2", global_msg)

    def test_group_fav_init_uses_configured_initial_level(self):
        self.plugin.initial_level = 37
        event = _FakeEvent(sender_id="u-init", sender_name="测试用户", group_id="g-init")

        async def _run():
            results = []
            async for out in self.plugin.cmd_fav_init(event):
                results.append(out)
            return results

        results = asyncio.run(_run())
        self.assertTrue(any("好感度: 37" in msg for msg in results))

        user = self.plugin.db.get_user("group", "g-init", "u-init")
        self.assertIsNotNone(user)
        assert user is not None
        self.assertEqual(user.level, 37)

    def test_group_auto_coerce_uses_configured_initial_level(self):
        self.plugin.initial_level = 26
        event = _FakeEvent(sender_id="u-auto", sender_name="自动用户", group_id="g-auto")
        req = _Req()

        asyncio.run(self.plugin.on_llm_request(event, req))

        user = self.plugin.db.get_user("group", "g-auto", "u-auto")
        self.assertIsNotNone(user)
        assert user is not None
        self.assertEqual(user.level, 26)

    def test_initialize_uses_plugin_data_db_path(self):
        with tempfile.TemporaryDirectory() as td:
            main_mod, _ = _load_plugin_module(data_path=td)
            config = {
                "min_level": {"value": -100},
                "max_level": {"value": 100},
                "initial_level": {"value": 12},
                "tiers": {
                    "value": json.dumps(
                        [
                            {"name": "敌对", "min": -100, "max": -51, "effect": "敌对"},
                            {"name": "冷淡", "min": -50, "max": -11, "effect": "冷淡"},
                            {"name": "中立", "min": -10, "max": 9, "effect": "中立"},
                            {"name": "友好", "min": 10, "max": 39, "effect": "友好"},
                            {"name": "亲密", "min": 40, "max": 100, "effect": "亲密"},
                        ],
                        ensure_ascii=False,
                    )
                },
            }
            plugin = main_mod.FavorabilityPlugin(None, config)
            asyncio.run(plugin.initialize())

            db_path = plugin.db.conn.execute("PRAGMA database_list").fetchone()[2]
            expected_db_path = os.path.join(
                td,
                "data",
                "plugin_data",
                "astrbot_plugin_favorability_system",
                "favorability.db",
            )
            self.assertEqual(
                os.path.normcase(os.path.normpath(db_path)),
                os.path.normcase(os.path.normpath(expected_db_path)),
            )
            plugin.db.close()


if __name__ == "__main__":
    unittest.main()
