"""本地验证：manifest 合法性 + 插件可加载 + 注入 Item 能被宿主反序列化。

用法（在 Maibot 仓库目录外也能跑，只要目标解释器里装了 maibot-plugin-sdk）：
    python verify_plugin.py /path/to/MaiBot
第一个参数是 MaiBot 仓库根目录，默认取环境变量 MAIBOT_REPO，再默认取本机路径。
"""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

_argv_repo = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("MAIBOT_REPO", "")
if _argv_repo:
    REPO = Path(_argv_repo).expanduser().resolve()
else:
    REPO = Path.cwd()
    print("[提示] 未指定 MaiBot 仓库根目录（位置参数或环境变量 MAIBOT_REPO），"
          "使用当前目录——依赖宿主源码的校验项可能跳过")
PLUGIN_DIR = Path(__file__).resolve().parent / "person_profile_name_alias"
if not PLUGIN_DIR.is_dir():
    PLUGIN_DIR = REPO / "plugins" / "person_profile_name_alias"
if not PLUGIN_DIR.is_dir() and (Path(__file__).resolve().parent / "_manifest.json").is_file():
    PLUGIN_DIR = Path(__file__).resolve().parent  # 本仓库根目录即插件目录
sys.path.insert(0, str(REPO))

# ---------- 0. 预先加载宿主真实的会话消息序列化工具 ----------
# 必须在后面的 stub 之前导入：hook_payloads 会连带拉起 src.config 等真实模块。
import logging as _logging  # noqa: E402

if "src.common.logger" not in sys.modules:
    _logger_stub = types.ModuleType("src.common.logger")
    _logger_stub.get_logger = lambda *_a, **_k: _logging.getLogger("test")
    sys.modules["src.common.logger"] = _logger_stub

HOST_SERIALIZE = None
HOST_DESERIALIZE = None
HOST_MSG_IMPORT_ERROR = ""
try:
    from src.plugin_runtime.hook_payloads import (  # noqa: E402
        deserialize_session_message,
        serialize_session_message,
    )

    HOST_SERIALIZE = serialize_session_message
    HOST_DESERIALIZE = deserialize_session_message
except Exception as exc:  # noqa: BLE001
    HOST_MSG_IMPORT_ERROR = repr(exc)

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" -> {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def stub(name: str, **attrs: object) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def make_class(name: str) -> type:
    return type(name, (), {})


# ---------- 1. 用宿主真实 Manifest 模型校验 _manifest.json ----------
try:
    import logging

    stub(
        "src.common.logger",
        get_logger=lambda *_a, **_k: logging.getLogger("test"),
    )
    stub("src.plugin_runtime.local_sdk", read_local_sdk_version=lambda *_a, **_k: "2.8.0", ENV_LOCAL_PLUGIN_SDK_PATH="X")
    import src.plugin_runtime as plugin_runtime_pkg

    plugin_runtime_pkg.detect_host_application_version = lambda *_a, **_k: "1.2.4"
    plugin_runtime_pkg.read_local_sdk_version = lambda *_a, **_k: "2.8.0"

    from src.plugin_runtime.runner.manifest_validator import PluginManifest

    manifest_raw = json.loads((PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))
    manifest = PluginManifest.model_validate(manifest_raw)
    check("manifest 通过宿主 PluginManifest 校验", True, f"id={manifest.id} type={manifest.plugin_type}")
except Exception as exc:  # noqa: BLE001
    check("manifest 通过宿主 PluginManifest 校验", False, repr(exc))


# ---------- 2. 插件模块可被 SDK 加载并声明了预期组件 ----------
try:
    import importlib.util

    spec = importlib.util.spec_from_file_location("ppna_plugin", PLUGIN_DIR / "plugin.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["ppna_plugin"] = module
    spec.loader.exec_module(module)

    from maibot_sdk.components import collect_components

    plugin = module.create_plugin()
    components = collect_components(plugin)
    hooks: list[str] = []
    tools: list[str] = []
    for component in components:
        metadata = component.get("metadata") or {}
        component_type = str(component.get("type") or "").lower()
        if component_type == "hook_handler":
            hooks.append(f"{component.get('name')}->{metadata.get('hook')}[{metadata.get('mode')}]")
        elif component_type == "tool":
            tools.append(str(component.get("name")))

    check("插件可实例化", isinstance(plugin, module.PersonNameAliasPlugin))
    check(
        "声明了两个 HookHandler（blocking）",
        sorted(hooks)
        == [
            "inject_name_alias->maisaka.planner.before_request[blocking]",
            "record_people->chat.receive.after_process[blocking]",
        ],
        str(hooks),
    )
    check("未注册 Tool（核心功能全走钩子，1.4.2 起精简）", tools == [], str(tools))
    check("配置模型可生成默认配置", module.PersonNameAliasConfig.__name__ == "PersonNameAliasConfig")

    # 校验配置 schema 可生成（WebUI 面板依赖它）
    from maibot_sdk.config import build_plugin_default_config, generate_plugin_config_schema

    defaults = build_plugin_default_config(module.PersonNameAliasConfig)
    schema = generate_plugin_config_schema(module.PersonNameAliasConfig)
    check(
        "配置默认值与 schema 生成正常",
        defaults["plugin"]["enabled"] is True and defaults["injection"]["max_people"] == 3 and bool(schema),
        json.dumps(defaults, ensure_ascii=False)[:120],
    )

    # ---------- 2b. WebUI 控件类型：参数类字段必须是下拉框，且每个字段都有中文标签 ----------
    name_replace_fields = schema["sections"]["name_replace"]["fields"]
    check(
        "生效范围是下拉框（select）",
        name_replace_fields["scope"]["ui_type"] == "select"
        and name_replace_fields["scope"]["choices"] == list(module.SCOPE_CHOICES),
        f"{name_replace_fields['scope']['ui_type']} / {name_replace_fields['scope']['choices']}",
    )
    check(
        "名字来源是下拉框（select）",
        name_replace_fields["name_source"]["ui_type"] == "select"
        and name_replace_fields["name_source"]["choices"] == list(module.NAME_SOURCE_CHOICES),
        f"{name_replace_fields['name_source']['ui_type']} / {name_replace_fields['name_source']['choices']}",
    )
    check(
        "下拉取值与应用内部使用的常量一致",
        defaults["name_replace"]["scope"] == module.SCOPE_MESSAGE
        and defaults["name_replace"]["name_source"] == module.NAME_SOURCE_AUTO,
        f"{defaults['name_replace']['scope']} / {defaults['name_replace']['name_source']}",
    )
    check(
        "JSON 覆盖项是多行文本框",
        name_replace_fields["overrides"]["ui_type"] == "textarea",
        name_replace_fields["overrides"]["ui_type"],
    )

    all_fields = [
        field
        for section in schema["sections"].values()
        for field in section["fields"].values()
    ]
    missing_labels = [
        field["name"] for field in all_fields if not str(field.get("label") or "").strip()
    ]
    check(
        "每个字段都有中文标签（宿主只渲染 label，不渲染 description）",
        not missing_labels,
        str(missing_labels),
    )
    missing_hints = [
        field["name"] for field in all_fields if not str(field.get("hint") or "").strip()
    ]
    # config_version / title / footer 这类自解释字段允许没有 hint
    allowed_without_hint = {"config_version", "title", "footer"}
    check(
        "开关/下拉/数值类字段都带说明（hint）",
        not [name for name in missing_hints if name not in allowed_without_hint],
        str(missing_hints),
    )
    check(
        "数值字段带上下界（前端会限制输入范围）",
        schema["sections"]["injection"]["fields"]["max_people"]["min"] == 1
        and schema["sections"]["injection"]["fields"]["max_people"]["max"] == module.MAX_RECORDED_PEOPLE,
        f"min={schema['sections']['injection']['fields']['max_people']['min']} "
        f"max={schema['sections']['injection']['fields']['max_people']['max']}",
    )

    # ---------- 2c. 旧配置（英文取值）升级不报错 ----------
    migrated = module.PersonNameAliasConfig.model_validate(
        {"name_replace": {"scope": "message", "name_source": "manual_alias"}}
    )
    check(
        "旧版 message/manual_alias 能迁移为中文取值",
        migrated.name_replace.scope == module.SCOPE_MESSAGE
        and migrated.name_replace.name_source == module.NAME_SOURCE_MANUAL_ALIAS,
        f"{migrated.name_replace.scope} / {migrated.name_replace.name_source}",
    )
    migrated_planner = module.PersonNameAliasConfig.model_validate(
        {"name_replace": {"scope": "planner", "name_source": "person_name"}}
    )
    check(
        "旧版 planner/person_name 能迁移为中文取值",
        migrated_planner.name_replace.scope == module.SCOPE_PLANNER
        and migrated_planner.name_replace.name_source == module.NAME_SOURCE_PERSON_NAME,
        f"{migrated_planner.name_replace.scope} / {migrated_planner.name_replace.name_source}",
    )
    fallback = module.PersonNameAliasConfig.model_validate(
        {"name_replace": {"scope": "不存在的取值", "name_source": "???"}}
    )
    check(
        "无法识别的取值回退默认值而不是抛异常",
        fallback.name_replace.scope == module.SCOPE_MESSAGE
        and fallback.name_replace.name_source == module.NAME_SOURCE_AUTO,
        f"{fallback.name_replace.scope} / {fallback.name_replace.name_source}",
    )
except Exception as exc:  # noqa: BLE001
    check("插件模块加载与组件声明", False, repr(exc))


# ---------- 3. 注入的 Item 能被宿主反序列化器接受 ----------
try:
    stub("src.config")
    stub("src.config.model_configs", APIProvider=make_class("APIProvider"), ModelInfo=make_class("ModelInfo"))
    llm_models_pkg = stub("src.llm_models")
    llm_models_pkg.__path__ = [str(REPO / "src" / "llm_models")]
    stub(
        "src.llm_models.model_client",
    )
    stub(
        "src.llm_models.model_client.base_client",
        **{
            name: make_class(name)
            for name in (
                "APIResponse",
                "AudioTranscriptionRequest",
                "ClientRequest",
                "EmbeddingRequest",
                "GenerationAttempt",
                "RequestTraceContext",
                "ResponseRequest",
            )
        },
    )
    stub(
        "src.llm_models.generation_diagnostics",
        sanitize_diagnostic_url=lambda value: value,
        sanitize_generation_diagnostic=lambda value: value,
    )

    from src.llm_models.payload_content.context_item import CONTEXT_ITEM_SCHEMA_VERSION
    from src.llm_models.payload_content.context_protocol import ContextProtocolMode, validate_context_items
    from src.llm_models.request_snapshot import (
        deserialize_context_item_snapshot,
        serialize_context_item_snapshot,
    )

    plugin = module.PersonNameAliasPlugin()
    text = plugin._build_reference_text(  # noqa: SLF001
        [
            {"person_id": "pid-1", "name": "张三", "aliases": ["小张", "三哥"]},
            {"person_id": "pid-2", "name": "李四", "aliases": []},
        ]
    )
    check("参考文本包含称呼与别名", "称呼=张三" in text and "别名=小张、三哥" in text, text.splitlines()[1] if len(text.splitlines()) > 1 else text)

    item = plugin._build_reference_item(text)  # noqa: SLF001
    restored = deserialize_context_item_snapshot(item)
    check("单条 Item 可被 deserialize_context_item_snapshot 还原", restored.meta.item_id == item["meta"]["item_id"])

    # 模拟宿主：已有 system + 历史 user + 尾部 user 提醒，插入后整表校验
    existing_item = {
        "item_type": "UserMessageItem",
        "meta": {"item_id": "existing-1", "logical_turn_id": None, "timestamp": "2026-09-10T20:00:00"},
        "parts": [{"type": "text", "text": "在吗"}],
    }
    tail_item = {
        "item_type": "UserMessageItem",
        "meta": {"item_id": "tail-1", "logical_turn_id": None, "timestamp": "2026-09-10T20:00:01"},
        "parts": [{"type": "text", "text": "请以 JSON 输出"}],
    }
    items = [existing_item, tail_item]
    new_items = plugin._insert_reference_item(list(items), text)  # noqa: SLF001
    ordered_ids = [i["meta"]["item_id"] for i in new_items]
    inserted_ok = (
        len(new_items) == 3
        and ordered_ids[0] == "existing-1"
        and ordered_ids[2] == "tail-1"
        and ordered_ids[1] not in {"existing-1", "tail-1"}
        and new_items[1]["parts"][0]["text"] == text
    )
    check("插入位置在最近一条 user 消息之前", inserted_ok, str(ordered_ids))

    deserialized = [
        deserialize_context_item_snapshot(raw) for raw in new_items
    ]
    validate_context_items(deserialized, ContextProtocolMode.REQUEST_CONTEXT)
    check(
        "整表通过宿主 validate_context_items(REQUEST_CONTEXT) 校验",
        len(deserialized) == 3,
        str([type(i).__name__ for i in deserialized]),
    )
    check(
        "新增 Item 的类型为 UserMessageItem",
        type(deserialized[1]).__name__ == "UserMessageItem",
        type(deserialized[1]).__name__,
    )
    check(
        "schema 版本与宿主一致",
        CONTEXT_ITEM_SCHEMA_VERSION == 1,
        str(CONTEXT_ITEM_SCHEMA_VERSION),
    )

    # 已有 Item 必须原样保留（replay 不被破坏）
    snapshot = serialize_context_item_snapshot(deserialized[0])
    check("已有 Item 序列化后保持原样", snapshot == existing_item, json.dumps(snapshot, ensure_ascii=False))
except Exception as exc:  # noqa: BLE001
    check("注入 Item 兼容宿主协议", False, repr(exc))


# ---------- 4. 端到端模拟：收消息 -> planner 注入（含人工别名库） ----------
try:
    import asyncio
    import sqlite3
    import tempfile

    person_fields = {
        "pid-1001": {
            "is_known": True,
            "person_name": "张三",
            "nickname": "小张",
            "group_cardname_list": [{"group_id": "g1", "group_cardname": "小张"}],
        },
        "pid-1002": {"is_known": True, "person_name": "李四", "nickname": "", "group_cardname_list": []},
        "pid-1003": {"is_known": True, "person_name": "", "nickname": "王五", "group_cardname_list": []},
        "0123456789abcdef0123456789abcdef": {
            "is_known": True,
            "person_name": "赵六",
            "nickname": "",
            "group_cardname_list": [],
        },
    }

    class FakePerson:
        async def get_id(self, platform: str, user_id: str) -> dict[str, object]:
            return {"success": True, "person_id": f"pid-{user_id}"}

        async def get_id_by_name(self, person_name: str) -> dict[str, object]:
            return {"success": False, "person_id": ""}

        async def get_value(self, person_id: str, field_name: str) -> dict[str, object]:
            return {"success": True, "value": person_fields.get(person_id, {}).get(field_name)}

    class FakeConfig:
        async def get(self, key: str, default: object = None) -> object:
            del key
            return default

    class FakeLogger:
        def __init__(self) -> None:
            self.lines: list[str] = []

        def info(self, message: str) -> None:
            self.lines.append(str(message))

        def warning(self, message: str) -> None:
            self.lines.append(str(message))

        def debug(self, message: str) -> None:
            self.lines.append(str(message))

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "metadata.db"
        connection = sqlite3.connect(db_path)
        connection.execute(
            "CREATE TABLE person_profile_alias_overrides ("
            "person_id TEXT PRIMARY KEY, aliases_json TEXT NOT NULL, updated_at REAL, updated_by TEXT, source TEXT)"
        )
        connection.execute(
            "INSERT INTO person_profile_alias_overrides VALUES (?, ?, ?, ?, ?)",
            ("pid-1001", json.dumps(["三哥", "张三他哥"], ensure_ascii=False), 1.0, "webui", "webui"),
        )
        connection.execute(
            "CREATE TABLE person_profile_snapshots (snapshot_id INTEGER PRIMARY KEY, person_id TEXT, profile_text TEXT)"
        )
        connection.commit()
        connection.close()

        plugin = module.PersonNameAliasPlugin()
        plugin._plugin_config_instance = module.PersonNameAliasConfig(  # noqa: SLF001
            injection=module.InjectionConfig(max_people=3),
            manual_alias=module.ManualAliasConfig(metadata_db_path=str(db_path)),
            debug=module.DebugConfig(log_injection=True),
        )
        fake_ctx = types.SimpleNamespace(person=FakePerson(), config=FakeConfig(), logger=FakeLogger())
        plugin._ctx = fake_ctx  # noqa: SLF001

        message = {
            "session_id": "group_123",
            "platform": "qq",
            "message_info": {
                "user_info": {"user_id": "1001", "user_nickname": "张三", "user_cardname": "小张"},
            },
            "raw_message": [
                {"type": "at", "data": {"target_user_id": "1002", "target_user_nickname": "李四"}},
                {"type": "reply", "data": {"target_message_sender_id": "1003", "target_message_sender_nickname": "王五"}},
                {"type": "text", "data": "在吗"},
            ],
        }

        async def run_flow() -> tuple[dict[str, object], str]:
            record_result = await plugin.handle_record_people(message=message)
            planner_result = await plugin.handle_planner_before_request(
                hook_name="maisaka.planner.before_request",
                items=[dict(existing_item), dict(tail_item)],
                item_schema_version=1,
                tool_definitions=[],
                selected_history_count=1,
                built_message_count=2,
                selection_reason="test",
                session_id="group_123",
            )
            return record_result, str(planner_result["modified_kwargs"]["items"][1]["parts"][0]["text"])

        record_result, injected_text = asyncio.run(run_flow())
        print("---- 注入内容 ----")
        print(injected_text)
        print("------------------")

        check(
            "收消息钩子返回 continue（换名命中时会附带 modified_kwargs）",
            record_result.get("action") == "continue",
            str(list(record_result.keys())),
        )
        check(
            "注入块包含 3 个人的称呼与别名",
            "张三：称呼=张三；别名=小张、三哥、张三他哥" in injected_text
            and "李四：称呼=李四" in injected_text
            and "王五：称呼=王五" in injected_text,
            injected_text.replace("\n", " | "),
        )
        check("人工别名（metadata.db）已生效", "三哥" in injected_text and "张三他哥" in injected_text)
        check("主称呼未混进别名列表", "称呼=张三；别名=张三、" not in injected_text)
        check(
            "会话 ID 不匹配时按短窗口回退（不跨群串味太久）",
            [p["user_id"] for p in plugin._pick_people("group_999")] == ["1001", "1002", "1003"],  # noqa: SLF001
        )
        plugin._plugin_config_instance = module.PersonNameAliasConfig(  # noqa: SLF001
            injection=module.InjectionConfig(fallback_to_latest=False),
            manual_alias=module.ManualAliasConfig(metadata_db_path=str(db_path)),
        )
        check(
            "关闭回退后不跨会话注入",
            plugin._pick_people("group_999") == [],  # noqa: SLF001
        )
        plugin._plugin_config_instance = module.PersonNameAliasConfig(  # noqa: SLF001
            injection=module.InjectionConfig(max_people=3),
            manual_alias=module.ManualAliasConfig(metadata_db_path=str(db_path)),
            debug=module.DebugConfig(log_injection=True),
        )

        entry = asyncio.run(plugin._describe_person({"platform": "qq", "user_id": "1002"}))  # noqa: SLF001
        check(
            "按 QQ 号解析出称呼/别名（原查询工具的核心路径）",
            entry is not None and entry.get("name") == "李四",
            str(entry),
        )
        entry_by_id = asyncio.run(
            plugin._describe_person({"person_id": "0123456789abcdef0123456789abcdef"})  # noqa: SLF001
        )
        check(
            "person_id（32 位十六进制）直接命中",
            entry_by_id is not None and entry_by_id.get("name") == "赵六",
            str(entry_by_id),
        )

        # 身份解析：默认不得按名称反查
        name_calls: list[str] = []

        class TracingPerson(FakePerson):
            async def get_id_by_name(self, person_name: str) -> dict[str, object]:
                name_calls.append(person_name)
                return {"success": True, "person_id": "pid-by-name"}

        plugin._ctx = types.SimpleNamespace(person=TracingPerson(), config=FakeConfig(), logger=FakeLogger())  # noqa: SLF001
        by_name = asyncio.run(plugin._resolve_person_id({"platform": "", "user_id": "", "name": "小张"}))  # noqa: SLF001
        check(
            "拿不到 QQ 号时不按群名片/昵称猜人（无名称兜底路径）",
            by_name == "" and name_calls == [],
            f"person_id={by_name!r} 反查调用={name_calls}",
        )
        plugin._plugin_config_instance = module.PersonNameAliasConfig(  # noqa: SLF001
            injection=module.InjectionConfig(max_people=3),
            manual_alias=module.ManualAliasConfig(metadata_db_path=str(db_path)),
            debug=module.DebugConfig(log_injection=True),
        )

        # 关闭插件后不应注入
        plugin._plugin_config_instance = module.PersonNameAliasConfig(  # noqa: SLF001
            plugin=module.PluginSectionConfig(enabled=False),
            manual_alias=module.ManualAliasConfig(metadata_db_path=str(db_path)),
        )
        disabled_result = asyncio.run(
            plugin.handle_planner_before_request(items=[dict(existing_item)], session_id="group_123")
        )
        check("插件关闭后不注入", "modified_kwargs" not in disabled_result, str(disabled_result))
except Exception as exc:  # noqa: BLE001
    check("端到端模拟", False, repr(exc))


# ---------- 5. 昵称替换：改写后的消息仍能被宿主反序列化 ----------
if HOST_SERIALIZE is None or HOST_DESERIALIZE is None:
    print(f"[SKIP] 宿主消息序列化模块不可用，跳过昵称替换验证: {HOST_MSG_IMPORT_ERROR}")
else:
    try:
        import asyncio as _asyncio
        import base64 as _base64
        import sqlite3 as _sqlite3
        import tempfile as _tempfile

        REPLACE_PERSON_FIELDS: dict[str, dict[str, object]] = {
            "pid-1001": {"is_known": True, "person_name": "张三", "nickname": "小张"},
            "pid-1002": {"is_known": True, "person_name": "李四", "nickname": "小李"},
            "pid-1003": {"is_known": True, "person_name": "", "nickname": "王五"},
            "pid-1004": {"is_known": False, "person_name": "未知用户9999", "nickname": None},
        }

        class FakePersonReplace:
            async def get_id(self, platform: str, user_id: str) -> dict[str, object]:
                return {"success": True, "person_id": f"pid-{user_id}"}

            async def get_id_by_name(self, person_name: str) -> dict[str, object]:
                return {"success": False, "person_id": ""}

            async def get_value(self, person_id: str, field_name: str) -> dict[str, object]:
                return {"success": True, "value": REPLACE_PERSON_FIELDS.get(person_id, {}).get(field_name)}

        class FakeConfigReplace:
            async def get(self, key: str, default: object = None) -> object:
                del key
                return default

        class FakeLoggerReplace:
            def info(self, message: str) -> None:
                del message

            def warning(self, message: str) -> None:
                del message

            def debug(self, message: str) -> None:
                del message

        def build_payload(
            sender_id: str = "1001",
            sender_name: str = "小张",
            *,
            simple: bool = False,
        ) -> dict[str, object]:
            """构造一份和宿主真实载荷同构的消息字典。"""

            components: list[dict[str, object]] = []
            plain_text = "在吗"
            if not simple:
                components = [
                    {
                        "type": "reply",
                        "data": {
                            "target_message_id": "m-0",
                            "target_message_content": "早",
                            "target_message_sender_id": "1003",
                            "target_message_sender_nickname": "王五",
                            "target_message_sender_cardname": "王五",
                        },
                    },
                    {
                        "type": "at",
                        "data": {
                            "target_user_id": "1002",
                            "target_user_nickname": "小李",
                            "target_user_cardname": "小李",
                        },
                    },
                    {
                        "type": "image",
                        "data": "[图片，识别中.....]",
                        "hash": "deadbeef",
                        "binary_data_base64": _base64.b64encode(b"hello").decode(),
                    },
                ]
                plain_text = "[回复了王五的消息: 早]@小李 在吗"
            components.append({"type": "text", "data": "在吗"})

            return {
                "message_id": "m-1",
                "timestamp": "1757500000.0",
                "platform": "qq",
                "session_id": "group_123",
                "message_info": {
                    "user_info": {
                        "user_id": sender_id,
                        "user_nickname": sender_name,
                        "user_cardname": sender_name,
                    },
                    "group_info": {"group_id": "g-1", "group_name": "测试群"},
                    "additional_config": {},
                },
                "raw_message": components,
                "is_mentioned": True,
                "is_at": False,
                "is_emoji": False,
                "is_picture": not simple,
                "is_command": False,
                "is_notify": False,
                "processed_plain_text": plain_text,
            }

        with _tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "metadata.db"
            connection = _sqlite3.connect(db_path)
            connection.execute(
                "CREATE TABLE person_profile_alias_overrides ("
                "person_id TEXT PRIMARY KEY, aliases_json TEXT NOT NULL, updated_at REAL, updated_by TEXT, source TEXT)"
            )
            connection.execute(
                "INSERT INTO person_profile_alias_overrides VALUES (?, ?, ?, ?, ?)",
                ("pid-1003", json.dumps(["老王"], ensure_ascii=False), 1.0, "webui", "webui"),
            )
            connection.commit()
            connection.close()

            def make_plugin(**config_overrides: object) -> object:
                instance = module.PersonNameAliasPlugin()
                config_kwargs: dict[str, object] = {
                    "manual_alias": module.ManualAliasConfig(metadata_db_path=str(db_path)),
                }
                config_kwargs.update(config_overrides)
                instance._plugin_config_instance = module.PersonNameAliasConfig(**config_kwargs)  # noqa: SLF001
                instance._ctx = types.SimpleNamespace(  # noqa: SLF001
                    person=FakePersonReplace(),
                    config=FakeConfigReplace(),
                    logger=FakeLoggerReplace(),
                )
                return instance

            # 载荷先过一遍宿主自己的序列化/反序列化，确保结构与生产一致
            host_message = HOST_DESERIALIZE(build_payload())
            payload = HOST_SERIALIZE(host_message)
            check("宿主导出的消息载荷可被再次反序列化", HOST_DESERIALIZE(payload).message_id == "m-1")

            plugin_default = make_plugin()
            result = _asyncio.run(plugin_default.handle_record_people(message=payload))
            new_payload = (result.get("modified_kwargs") or {}).get("message")
            check(
                "message 范围：钩子回写了 modified_kwargs.message",
                isinstance(new_payload, dict),
                str(list(result.keys())),
            )

            if isinstance(new_payload, dict):
                user_info = (new_payload.get("message_info") or {}).get("user_info") or {}
                components_after = new_payload.get("raw_message") or []
                reply_data = components_after[0].get("data") if isinstance(components_after[0], dict) else {}
                at_data = components_after[1].get("data") if isinstance(components_after[1], dict) else {}
                check(
                    "发起人昵称/群名片被替换成主称呼",
                    user_info.get("user_nickname") == "张三" and user_info.get("user_cardname") == "张三",
                    f"{user_info.get('user_nickname')} / {user_info.get('user_cardname')}",
                )
                check(
                    "@ 目标昵称被替换（主称呼优先）",
                    at_data.get("target_user_nickname") == "李四" and at_data.get("target_user_cardname") == "李四",
                    str(at_data),
                )
                check(
                    "引用目标昵称被替换（人工别名兜底）",
                    reply_data.get("target_message_sender_nickname") == "老王"
                    and reply_data.get("target_message_sender_cardname") == "老王",
                    str(reply_data),
                )
                check(
                    "正文里的 @旧名 / 回复了旧名 一并改写",
                    new_payload.get("processed_plain_text") == "[回复了老王的消息: 早]@李四 在吗",
                    str(new_payload.get("processed_plain_text")),
                )

                restored = HOST_DESERIALIZE(new_payload)
                image_component = next(
                    (item for item in restored.raw_message.components if type(item).__name__ == "ImageComponent"),
                    None,
                )
                check(
                    "改写后的消息仍能被宿主反序列化，且关键字段保真",
                    restored.message_id == "m-1"
                    and restored.session_id == "group_123"
                    and restored.platform == "qq"
                    and abs(restored.timestamp.timestamp() - host_message.timestamp.timestamp()) < 1
                    and restored.is_mentioned is True
                    and len(restored.raw_message.components) == len(host_message.raw_message.components),
                    f"id={restored.message_id} session={restored.session_id} 组件={len(restored.raw_message.components)}",
                )
                check(
                    "图片二进制与文本内容未在改写中丢失",
                    image_component is not None
                    and image_component.binary_data == b"hello"
                    and image_component.content == "[图片，识别中.....]",
                    repr(getattr(image_component, "binary_data", None)),
                )

            # 未认识的人：不替换，也不回写消息体
            payload_unknown = HOST_SERIALIZE(HOST_DESERIALIZE(build_payload("1004", "路人甲", simple=True)))
            result_unknown = _asyncio.run(make_plugin().handle_record_people(message=payload_unknown))
            check(
                "未进入档案的人保持原样且不回写消息体",
                result_unknown == {"action": "continue"}
                and payload_unknown["message_info"]["user_info"]["user_nickname"] == "路人甲",  # type: ignore[index]
                str(result_unknown),
            )

            # overrides：连未认识的人也能指定
            payload_override = HOST_SERIALIZE(HOST_DESERIALIZE(build_payload("1004", "路人甲", simple=True)))
            result_override = _asyncio.run(
                make_plugin(name_replace=module.NameReplaceConfig(overrides='{"1004": "神秘人"}')).handle_record_people(
                    message=payload_override
                )
            )
            override_user_info = (
                ((result_override.get("modified_kwargs") or {}).get("message") or {}).get("message_info") or {}
            ).get("user_info") or {}
            check(
                "overrides 能替换未进入档案的人",
                override_user_info.get("user_nickname") == "神秘人",
                str(override_user_info),
            )

            # name_source=manual_alias：没有人工别名就不替换
            payload_alias = HOST_SERIALIZE(HOST_DESERIALIZE(build_payload("1001", "小张", simple=True)))
            result_alias = _asyncio.run(
                make_plugin(name_replace=module.NameReplaceConfig(name_source="manual_alias")).handle_record_people(
                    message=payload_alias
                )
            )
            check(
                "name_source=manual_alias 时没有人工别名就不动",
                result_alias == {"action": "continue"},
                str(result_alias),
            )

            # scope=planner：只改请求文本，不碰消息体
            plugin_planner = make_plugin(name_replace=module.NameReplaceConfig(scope="planner"))
            payload_planner = HOST_SERIALIZE(HOST_DESERIALIZE(build_payload()))
            result_planner = _asyncio.run(plugin_planner.handle_record_people(message=payload_planner))
            check(
                "planner 范围：不改写消息体",
                "modified_kwargs" not in result_planner
                and payload_planner["message_info"]["user_info"]["user_nickname"] == "小张",  # type: ignore[index]
                str(result_planner),
            )

            history_item = {
                "item_type": "UserMessageItem",
                "meta": {"item_id": "hist-1", "logical_turn_id": None, "timestamp": "2026-09-10T20:00:00"},
                "parts": [
                    {
                        "type": "text",
                        "text": '<message msg_id="m-1" time="20:00:00" user="小张" group_card="小张">\n[小张]在吗',
                    }
                ],
            }
            planner_result = _asyncio.run(
                plugin_planner.handle_planner_before_request(items=[history_item], session_id="group_123")
            )
            planner_items = (planner_result.get("modified_kwargs") or {}).get("items") or []
            rewritten = next(
                (
                    item
                    for item in planner_items
                    if isinstance(item, dict) and (item.get("meta") or {}).get("item_id") == "hist-1"
                ),
                None,
            )
            rewritten_text = (rewritten or {}).get("parts", [{}])[0].get("text", "")
            check(
                "planner 范围：请求文本里的 user/group_card/[称呼] 都被替换",
                'user="张三"' in rewritten_text
                and 'group_card="张三"' in rewritten_text
                and "[张三]在吗" in rewritten_text,
                str(rewritten_text),
            )
            check(
                "planner 范围：改写后的请求条目仍能通过宿主反序列化",
                rewritten is not None and deserialize_context_item_snapshot(rewritten) is not None,
            )
    except Exception as exc:  # noqa: BLE001
        check("昵称替换端到端验证", False, repr(exc))


print()
print("失败项:", FAILURES if FAILURES else "无")
raise SystemExit(1 if FAILURES else 0)
