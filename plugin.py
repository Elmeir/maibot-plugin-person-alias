"""人物称呼与别名插件。

作用：
    1. 注入：把「主称呼 + 别名」注入 Maisaka planner 的请求上下文，让麦麦在多人群聊里
       知道当前在说谁、这个人都有哪些叫法（包含 WebUI 里维护的人工别名）。
    2. 换名：把运行时里显示的 QQ 昵称 / 群名片替换成你自己维护的称呼，这样 planner、
       回复器、日志、WebUI 监控面板看到的名字都是你设定过的那个。

数据来源：
    1. 人物主档案：person.person_name / nickname / group_cardname_list
    2. A_Memorix 人物画像的人工别名覆盖表（只读方式打开 metadata.db，可选）

实现方式：
    - 订阅 chat.receive.after_process：记录最近发言、@、引用涉及的人物；
      换名开启时顺带回写序列化消息里的昵称/群名片字段。
    - 订阅 maisaka.planner.before_request：在请求发往模型前插入一条内部参考消息；
      换名范围为 planner 时，在这里改写请求文本里的称呼。
    全程只使用插件 SDK 能力，不修改宿主任何代码。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import field_validator

from maibot_sdk import Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import (
    ErrorPolicy,
    HookMode,
    HookOrder,
)

MAX_RECORDED_PEOPLE = 8
"""单次消息最多记录的人物数量。"""

SUPPORTED_CONFIG_VERSION = "1.0.0"
"""插件支持的配置版本（config_version 字段默认值，宿主据此触发字段级迁移）。"""

FALLBACK_WINDOW_SECONDS = 45
"""会话 ID 对不上时，允许回退到最近一次记录的时间窗口（避免跨群串味）。"""

SCOPE_MESSAGE = "入站即改写"
"""换名范围：入站即改写消息体，planner / 回复器 / 日志 / WebUI 全链路生效。"""

SCOPE_PLANNER = "仅改 planner"
"""换名范围：只改写 planner 请求文本，不碰消息体与数据库。"""

SCOPE_CHOICES: tuple[str, ...] = (SCOPE_MESSAGE, SCOPE_PLANNER)
"""生效范围的可选值（WebUI 下拉框用，写成中文，避免手打标识符出错）。"""

NAME_SOURCE_AUTO = "自动"
"""名字来源：主称呼优先，未自定义时退回第一个人工别名。"""

NAME_SOURCE_PERSON_NAME = "仅主称呼"
"""名字来源：只用人物主档案的 person_name。"""

NAME_SOURCE_MANUAL_ALIAS = "仅人工别名"
"""名字来源：只用 A_Memorix 里人工维护的别名（取第一个）。"""

NAME_SOURCE_CHOICES: tuple[str, ...] = (
    NAME_SOURCE_AUTO,
    NAME_SOURCE_PERSON_NAME,
    NAME_SOURCE_MANUAL_ALIAS,
)
"""名字来源的可选值（WebUI 下拉框用）。"""

_LEGACY_CHOICE_ALIASES: dict[str, str] = {
    "message": SCOPE_MESSAGE,
    "planner": SCOPE_PLANNER,
    "auto": NAME_SOURCE_AUTO,
    "personname": NAME_SOURCE_PERSON_NAME,
    "manualalias": NAME_SOURCE_MANUAL_ALIAS,
    "default": NAME_SOURCE_AUTO,
    "入站即改写全链路": SCOPE_MESSAGE,
    "主称呼": NAME_SOURCE_PERSON_NAME,
    "人工别名": NAME_SOURCE_MANUAL_ALIAS,
}
"""旧版配置值（英文标识符）到当前中文取值的映射，保证升级不炸配置。"""


def _ui_meta(label: str, hint: str = "", **extra: Any) -> dict[str, Any]:
    """构造字段的 WebUI 元数据。

    WebUI 的插件配置页只渲染 ``label`` 与 ``hint``（``description`` 不展示），
    因此每个字段都要显式给出中文标签与说明。

    Args:
        label: 字段显示名。
        hint: 字段下方的说明文字。
        **extra: 其它扩展项，如 ``x-widget``（强制控件类型）等。

    Returns:
        Dict[str, Any]: 字段扩展元数据。
    """

    meta: dict[str, Any] = {"label": label}
    if hint:
        meta["hint"] = hint
    meta.update(extra)
    return meta


def _normalize_choice(value: Any, choices: tuple[str, ...], default: str) -> str:
    """把配置里的取值归一化到 ``choices`` 之一。

    兼容三类输入：当前中文取值、旧版英文标识符、忽略空格大小写差异的写法；
    无法识别时回退到默认值（而不是抛异常，避免旧配置直接加载失败）。

    Args:
        value: 原始配置值。
        choices: 合法取值集合。
        default: 兜底取值。

    Returns:
        str: 归一化后的取值。
    """

    text = str(value or "").strip()
    if not text:
        return default
    if text in choices:
        return text

    compact = text.lower().replace(" ", "").replace("_", "").replace("-", "")
    for choice in choices:
        if choice.lower().replace(" ", "") == compact:
            return choice
    return _LEGACY_CHOICE_ALIASES.get(compact, default)


def _first_text(*values: Any) -> str:
    """返回第一个非空文本。"""

    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _coerce_alias_list(raw: Any) -> list[str]:
    """把别名原始值规范为去重后的字符串列表。"""

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    if not isinstance(raw, (list, tuple, set)):
        return []

    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        alias = str(item or "").strip()
        key = alias.casefold()
        if not alias or key in seen:
            continue
        seen.add(key)
        result.append(alias)
    return result


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="是否启用插件",
        json_schema_extra=_ui_meta("启用插件", "关闭后注入与昵称替换都不会执行"),
    )
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="配置版本",
        json_schema_extra=_ui_meta("配置版本", "插件自动维护，一般不需要修改", hidden=True, disabled=True),
    )


class InjectionConfig(PluginConfigBase):
    """注入行为配置。"""

    __ui_label__ = "注入"
    __ui_icon__ = "sparkles"
    __ui_order__ = 1

    max_people: int = Field(
        default=3,
        ge=1,
        le=MAX_RECORDED_PEOPLE,
        description="每轮最多注入几个人物",
        json_schema_extra=_ui_meta("每轮最多注入人数", f"1~{MAX_RECORDED_PEOPLE}，按最近发言顺序取前几位"),
    )
    enabled: bool = Field(
        default=True,
        description="启用注入（关闭后不注入人物参考块，昵称替换不受影响）",
        json_schema_extra=_ui_meta("启用注入", "关闭后不再向 planner 注入人物称呼参考块；昵称替换开关独立生效"),
    )
    include_aliases: bool = Field(
        default=True,
        description="是否注入别名（关闭则只注入称呼）",
        json_schema_extra=_ui_meta(
            "注入别名",
            "关闭后只注入称呼；称呼与消息里的原名相同、且无别名可注入的人物不再注入（避免零信息量的占位块）",
        ),
    )
    watch_window_seconds: int = Field(
        default=300,
        ge=5,
        description="候选人物保留时长（秒）",
        json_schema_extra=_ui_meta("候选保留时长（秒）", "超过该时长没有被提到的候选人物会被丢弃"),
    )
    fallback_to_latest: bool = Field(
        default=True,
        description="会话 ID 对不上时，回退使用最近 45 秒内记录的人物",
        json_schema_extra=_ui_meta("会话不匹配时回退", f"会话 ID 对不上时，回退使用最近 {FALLBACK_WINDOW_SECONDS} 秒内记录的人物"),
    )
    cache_seconds: int = Field(
        default=60,
        ge=0,
        description="人物信息缓存时长（秒）",
        json_schema_extra=_ui_meta("人物信息缓存（秒）", "缓存人物档案查询结果，减少重复查询"),
    )
    title: str = Field(
        default="【人物称呼与别名-内部参考】",
        description="注入块标题",
        json_schema_extra=_ui_meta("注入块标题"),
    )
    footer: str = Field(
        default="以上仅供内部推理使用，用于确认对话中提到的对象；不要向用户复述本段内容。",
        description="注入块结尾说明",
        json_schema_extra=_ui_meta("注入块结尾说明"),
    )


class ManualAliasConfig(PluginConfigBase):
    """人工别名配置。"""

    __ui_label__ = "人工别名"
    __ui_icon__ = "database"
    __ui_order__ = 2

    use_manual_aliases: bool = Field(
        default=True,
        description="读取 A_Memorix 里人工维护的别名",
        json_schema_extra=_ui_meta("读取人工别名", "读取你在 WebUI「别名维护」里保存的别名（只读，不改数据）"),
    )
    metadata_db_path: str = Field(
        default="",
        description="metadata.db 的绝对路径；留空时自动探测（data/a-memorix/metadata/metadata.db）",
        json_schema_extra=_ui_meta(
            "metadata.db 路径",
            "留空自动探测 data/a-memorix/metadata/metadata.db；也可填绝对路径",
            placeholder="留空 = 自动探测",
        ),
    )
    cache_seconds: int = Field(
        default=30,
        ge=0,
        description="人工别名缓存时长（秒）",
        json_schema_extra=_ui_meta("人工别名缓存（秒）", "人工别名读库结果的缓存时长"),
    )


class NameReplaceConfig(PluginConfigBase):
    """QQ 昵称替换配置。"""

    __ui_label__ = "昵称替换"
    __ui_icon__ = "user-round"
    __ui_order__ = 3

    enabled: bool = Field(
        default=True,
        description="启用：用画像里的称呼替换运行时显示的 QQ 昵称",
        json_schema_extra=_ui_meta("启用昵称替换", "用画像里的称呼替换运行时显示的 QQ 昵称/群名片"),
    )
    scope: Literal[SCOPE_MESSAGE, SCOPE_PLANNER] = Field(
        default=SCOPE_MESSAGE,
        description="生效范围",
        json_schema_extra=_ui_meta(
            "生效范围",
            "入站即改写 = planner/回复器/日志/WebUI 全链路都换名；仅改 planner = 只改发往模型的请求文本",
        ),
    )
    name_source: Literal[
        NAME_SOURCE_AUTO, NAME_SOURCE_PERSON_NAME, NAME_SOURCE_MANUAL_ALIAS
    ] = Field(
        default=NAME_SOURCE_AUTO,
        description="名字来源",
        json_schema_extra=_ui_meta(
            "名字来源",
            "自动 = 主称呼优先，主档案没自定义时用第一个人工别名",
        ),
    )
    replace_nickname: bool = Field(
        default=True,
        description="替换 QQ 昵称字段（message 范围）",
        json_schema_extra=_ui_meta("替换 QQ 昵称", "只对「入站即改写」范围有效"),
    )
    replace_cardname: bool = Field(
        default=True,
        description="替换群名片字段（群聊显示名以群名片优先，关掉可能导致替换看不到效果）",
        json_schema_extra=_ui_meta("替换群名片", "群聊显示名以群名片优先，关掉很可能看不到替换效果"),
    )
    replace_targets: bool = Field(
        default=True,
        description="同时替换 @ 与引用里被指向人的昵称",
        json_schema_extra=_ui_meta("替换 @ 与引用对象", "消息里 @ 到的人、被引用消息的发送者一起换名"),
    )
    rewrite_plain_text: bool = Field(
        default=True,
        description="同时改写正文里的「@旧名」「回复了旧名的消息」这两类固定格式",
        json_schema_extra=_ui_meta("改写正文里的 @旧名", "改写纯文本里的「@旧名」「回复了旧名的消息」两类固定格式"),
    )
    overrides: str = Field(
        default="",
        description='硬编码覆盖，JSON 形如 {"qq:123456": "老王"}，优先级最高（找不到人物档案时也能用）',
        json_schema_extra=_ui_meta(
            "硬编码覆盖（JSON）",
            '形如 {"qq:123456": "老王"}；优先级最高，没进人物档案的人也能指定',
            **{"x-widget": "textarea", "placeholder": '{"qq:123456": "老王"}', "rows": 4},
        ),
    )

    @field_validator("scope", mode="before")
    @classmethod
    def _coerce_scope(cls, value: Any) -> str:
        """把旧的 message / planner 写法迁移到当前中文取值。"""

        return _normalize_choice(value, SCOPE_CHOICES, SCOPE_MESSAGE)

    @field_validator("name_source", mode="before")
    @classmethod
    def _coerce_name_source(cls, value: Any) -> str:
        """把旧的 auto / person_name / manual_alias 写法迁移到当前中文取值。"""

        return _normalize_choice(value, NAME_SOURCE_CHOICES, NAME_SOURCE_AUTO)


class DebugConfig(PluginConfigBase):
    """调试配置。"""

    __ui_label__ = "调试"
    __ui_icon__ = "terminal"
    __ui_order__ = 4

    log_injection: bool = Field(
        default=False,
        description="在日志中打印每次注入内容",
        json_schema_extra=_ui_meta("打印注入内容", "把每次注入给 planner 的文本打进日志"),
    )
    log_replace: bool = Field(
        default=False,
        description="在日志中打印每次昵称替换结果",
        json_schema_extra=_ui_meta("打印换名结果", "把每次昵称替换的对照关系打进日志"),
    )
    log_level_debug: bool = Field(
        default=False,
        description="打印更多调试日志",
        json_schema_extra=_ui_meta("更多调试日志", "排查问题时打开"),
    )


class PersonNameAliasConfig(PluginConfigBase):
    """插件总配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    injection: InjectionConfig = Field(default_factory=InjectionConfig)
    manual_alias: ManualAliasConfig = Field(default_factory=ManualAliasConfig)
    name_replace: NameReplaceConfig = Field(default_factory=NameReplaceConfig)
    debug: DebugConfig = Field(default_factory=DebugConfig)


class PersonNameAliasPlugin(MaiBotPlugin):
    """人物称呼与别名注入插件。"""

    config_model = PersonNameAliasConfig

    def __init__(self) -> None:
        super().__init__()
        self._recent: dict[str, dict[str, Any]] = {}
        self._person_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._manual_cache: dict[str, Any] = {"ts": 0.0, "data": None, "path": None}
        self._person_id_cache: dict[str, tuple[float, str]] = {}
        self._override_cache: dict[str, Any] = {"raw": None, "data": {}}

    # ------------------------------------------------------------------ 工具

    def _opt(self, section: str, field_name: str, default: Any) -> Any:
        """安全读取插件配置项。"""

        try:
            section_obj = getattr(self.config, section)
            value = getattr(section_obj, field_name)
        except Exception:
            return default
        return default if value is None else value

    def _log_debug(self, message: str) -> None:
        if bool(self._opt("debug", "log_level_debug", False)):
            self.ctx.logger.info(message)

    def _log_info(self, message: str) -> None:
        self.ctx.logger.info(message)

    def _log_warning(self, message: str) -> None:
        self.ctx.logger.warning(message)

    # -------------------------------------------------------------- 生命周期

    async def on_load(self) -> None:
        """插件加载。"""

        self._log_info(
            "人物称呼与别名注入插件已加载"
            f"（总开关={bool(self._opt('plugin', 'enabled', True))}"
            f"，昵称替换={self._name_replace_enabled()}"
            f"，生效范围={self._name_replace_scope()}"
            f"，名字来源={self._name_replace_source()}"
            f"，调试日志={bool(self._opt('debug', 'log_level_debug', False))}）"
        )
        path = await self._resolve_metadata_db()
        if path is None:
            self._log_info("未找到 A_Memorix metadata.db，将只使用人物主档案里的称呼与别名")
        else:
            self._log_info(f"已定位 A_Memorix metadata.db: {path}")

    async def on_unload(self) -> None:
        """插件卸载。"""

        self._recent.clear()
        self._person_cache.clear()
        self._person_id_cache.clear()
        self._log_info("人物称呼与别名注入插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """配置热重载。Runner 在通知前已自行应用新配置，这里只需清各类缓存。

        ``_recent`` 一并清空：里面记录的 ``replace`` 字段是旧配置下算出的，
        切换换名开关后最长 5 分钟内会以旧语义参与注入判定。
        """

        del scope, config_data, version
        self._recent.clear()
        self._person_cache.clear()
        self._person_id_cache.clear()
        self._manual_cache = {"ts": 0.0, "data": None, "path": None}
        self._override_cache = {"raw": None, "data": {}}

    # ------------------------------------------------------------ 人物记录

    @HookHandler(
        "chat.receive.after_process",
        name="record_people",
        description="记录最近发言、@、引用涉及的人物；换名开启时回写昵称字段",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=5000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_record_people(self, message: Any = None, **kwargs: Any) -> dict[str, Any]:
        """记录消息中涉及的人物，并按配置把 QQ 昵称替换成画像称呼。"""

        if not bool(self._opt("plugin", "enabled", True)) or not isinstance(message, dict):
            return {"action": "continue"}

        try:
            people, rewritten = await self._process_incoming_message(message)
        except Exception as exc:
            self._log_warning(f"记录人物/替换昵称失败，已跳过: {exc}")
            return {"action": "continue"}

        session_id = str(message.get("session_id") or "").strip()
        if session_id and people:
            self._recent[session_id] = {
                "people": self._dedupe_people(people)[:MAX_RECORDED_PEOPLE],
                "ts": time.time(),
            }
            self._prune_recent()

        if not rewritten:
            return {"action": "continue"}
        return {"action": "continue", "modified_kwargs": {**kwargs, "message": message}}

    def _name_replace_enabled(self) -> bool:
        """昵称替换是否启用。"""

        return bool(self._opt("name_replace", "enabled", True))

    def _name_replace_scope(self) -> str:
        """昵称替换的生效范围。"""

        return _normalize_choice(
            self._opt("name_replace", "scope", SCOPE_MESSAGE),
            SCOPE_CHOICES,
            SCOPE_MESSAGE,
        )

    def _name_replace_source(self) -> str:
        """昵称替换的名字来源。"""

        return _normalize_choice(
            self._opt("name_replace", "name_source", NAME_SOURCE_AUTO),
            NAME_SOURCE_CHOICES,
            NAME_SOURCE_AUTO,
        )

    async def _process_incoming_message(self, message: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
        """解析消息涉及的人物，必要时把 QQ 昵称改写为画像称呼。

        Returns:
            tuple[list[dict[str, Any]], bool]: 人物列表（含 ``replace`` 字段）以及消息体是否被改写。
        """

        replace_enabled = self._name_replace_enabled()
        mutate = replace_enabled and self._name_replace_scope() == SCOPE_MESSAGE
        replace_nickname = bool(self._opt("name_replace", "replace_nickname", True))
        replace_cardname = bool(self._opt("name_replace", "replace_cardname", True))
        replace_targets = bool(self._opt("name_replace", "replace_targets", True))
        platform = str(message.get("platform") or "").strip()

        people: list[dict[str, Any]] = []
        renamed: dict[str, str] = {}
        changed = False

        message_info = message.get("message_info")
        user_info = message_info.get("user_info") if isinstance(message_info, dict) else None
        user_info = user_info if isinstance(user_info, dict) else {}
        sender_id = str(user_info.get("user_id") or "").strip()
        if sender_id:
            original = _first_text(user_info.get("user_cardname"), user_info.get("user_nickname"))
            new_name = await self._resolve_display_name(platform, sender_id, original) if replace_enabled else ""
            people.append({"platform": platform, "user_id": sender_id, "name": original, "replace": new_name})
            if mutate and new_name:
                if replace_nickname and str(user_info.get("user_nickname") or "").strip() != new_name:
                    user_info["user_nickname"] = new_name
                    changed = True
                if replace_cardname and str(user_info.get("user_cardname") or "").strip() != new_name:
                    user_info["user_cardname"] = new_name
                    changed = True
                if original:
                    renamed[original] = new_name

        components = message.get("raw_message")
        for component in components if isinstance(components, list) else []:
            if not isinstance(component, dict):
                continue
            component_type = str(component.get("type") or "").strip().lower()
            data = component.get("data")
            if not isinstance(data, dict):
                continue

            if component_type == "at":
                target_id = str(data.get("target_user_id") or "").strip()
                nickname_key, cardname_key = "target_user_nickname", "target_user_cardname"
            elif component_type == "reply":
                target_id = str(data.get("target_message_sender_id") or "").strip()
                nickname_key, cardname_key = "target_message_sender_nickname", "target_message_sender_cardname"
            else:
                continue

            if not target_id:
                continue

            original = _first_text(data.get(cardname_key), data.get(nickname_key))
            new_name = await self._resolve_display_name(platform, target_id, original) if replace_enabled else ""
            people.append({"platform": platform, "user_id": target_id, "name": original, "replace": new_name})

            if not (mutate and replace_targets and new_name):
                continue
            if replace_nickname and str(data.get(nickname_key) or "").strip() != new_name:
                data[nickname_key] = new_name
                changed = True
            if replace_cardname and str(data.get(cardname_key) or "").strip() != new_name:
                data[cardname_key] = new_name
                changed = True
            if original:
                renamed[original] = new_name

        if mutate and bool(self._opt("name_replace", "rewrite_plain_text", True)) and renamed:
            original_text = message.get("processed_plain_text")
            if isinstance(original_text, str) and original_text:
                patched = original_text
                for old_name, new_name in renamed.items():
                    if not old_name or old_name == new_name:
                        continue
                    patched = patched.replace(f"@{old_name}", f"@{new_name}")
                    patched = patched.replace(f"回复了{old_name}的消息", f"回复了{new_name}的消息")
                if patched != original_text:
                    message["processed_plain_text"] = patched
                    changed = True

        if changed and bool(self._opt("debug", "log_replace", False)):
            summary = "、".join(f"{old}->{new}" for old, new in renamed.items()) or "（无）"
            self._log_info(f"已替换昵称: {summary}")

        return people, changed

    @staticmethod
    def _dedupe_people(people: list[dict[str, str]]) -> list[dict[str, str]]:
        """按 platform + user_id 去重，保留顺序。"""

        result: list[dict[str, str]] = []
        seen: set[str] = set()
        for person in people:
            key = f"{person.get('platform', '')}:{person.get('user_id', '')}"
            if not person.get("user_id") or key in seen:
                continue
            seen.add(key)
            result.append(person)
        return result

    def _prune_recent(self) -> None:
        """清理过期的候选人物记录。"""

        window = max(10, int(self._opt("injection", "watch_window_seconds", 300)))
        deadline = time.time() - window
        for key in [key for key, item in self._recent.items() if float(item.get("ts") or 0.0) < deadline]:
            self._recent.pop(key, None)

    def _pick_people(self, session_id: str) -> list[dict[str, str]]:
        """取出当前会话的候选人物；会话 ID 对不上时按短窗口回退。"""

        self._prune_recent()
        exact = self._recent.get(session_id)
        if exact and exact.get("people"):
            return list(exact["people"])

        if not bool(self._opt("injection", "fallback_to_latest", True)):
            return []

        window = max(10, int(self._opt("injection", "watch_window_seconds", 300)))
        deadline = time.time() - min(window, FALLBACK_WINDOW_SECONDS)
        newest: dict[str, Any] | None = None
        for item in self._recent.values():
            if float(item.get("ts") or 0.0) < deadline:
                continue
            if newest is None or float(item.get("ts") or 0.0) > float(newest.get("ts") or 0.0):
                newest = item
        if newest and newest.get("people"):
            self._log_debug(f"会话 {session_id!r} 无记录，回退到最近一次人物记录")
            return list(newest["people"])
        return []

    # ------------------------------------------------------------ 昵称替换

    async def _person_id_for(self, platform: str, user_id: str) -> str:
        """由 platform + user_id 解析 person_id（带缓存）。"""

        if not platform or not user_id:
            return ""

        cache_key = f"{platform}:{user_id}"
        cache_seconds = max(1, int(self._opt("injection", "cache_seconds", 60)))
        cached = self._person_id_cache.get(cache_key)
        if cached is not None and time.time() - cached[0] < cache_seconds:
            return cached[1]

        person_id = ""
        try:
            reply = await self.ctx.person.get_id(platform, user_id)
            if isinstance(reply, dict):
                if reply.get("success", True):
                    person_id = str(reply.get("person_id") or "").strip()
                else:
                    self._log_debug(f"person.get_id 被宿主拒绝: {cache_key} error={reply.get('error')!r}")
            elif reply:
                person_id = str(reply).strip()
        except Exception as exc:
            self._log_debug(f"person.get_id 调用异常: {cache_key} err={exc!r}")
        if not person_id:
            self._log_debug(f"person.get_id 未取到 person_id: {cache_key}")

        self._person_id_cache[cache_key] = (time.time(), person_id)
        return person_id

    def _load_overrides(self) -> dict[str, str]:
        """解析硬编码覆盖配置，带缓存。"""

        raw = str(self._opt("name_replace", "overrides", "") or "").strip()
        if self._override_cache.get("raw") == raw:
            return dict(self._override_cache.get("data") or {})

        data: dict[str, str] = {}
        if raw:
            try:
                parsed = json.loads(raw)
            except Exception as exc:
                self._log_warning(f"昵称替换 overrides 不是合法 JSON，已忽略: {exc}")
                parsed = None
            if isinstance(parsed, dict):
                for key, value in parsed.items():
                    name = str(value or "").strip()
                    if name:
                        data[str(key).strip()] = name

        self._override_cache = {"raw": raw, "data": data}
        return dict(data)

    def _lookup_override(self, platform: str, user_id: str) -> str:
        """按 platform:user_id、user_id 的顺序查硬编码覆盖。"""

        overrides = self._load_overrides()
        if not overrides:
            return ""
        for key in (f"{platform}:{user_id}", user_id):
            if key and key in overrides:
                return overrides[key]
        return ""

    def _first_manual_alias(self, person_id: str) -> str:
        """取人工维护别名的第一个（同步读取本地缓存）。"""

        if not person_id or not bool(self._opt("manual_alias", "use_manual_aliases", True)):
            return ""
        cached = self._manual_cache.get("data")
        if not isinstance(cached, dict):
            return ""
        aliases = cached.get(person_id)
        if isinstance(aliases, (list, tuple)) and aliases:
            return str(aliases[0] or "").strip()
        return ""

    async def _resolve_display_name(self, platform: str, user_id: str, original_name: str) -> str:
        """解析某人应该显示的名字；无需替换时返回空字符串。"""

        if not user_id:
            return ""

        key = str(original_name or "").strip()

        override = self._lookup_override(platform, user_id)
        if override:
            return "" if override.casefold() == key.casefold() else override

        person_id = await self._person_id_for(platform, user_id)
        if not person_id:
            return ""

        if not bool(await self._person_field(person_id, "is_known")):
            self._log_debug(f"跳过未认识的人物: {platform}:{user_id} person_id={person_id}")
            return ""

        person_name = str(await self._person_field(person_id, "person_name") or "").strip()
        # 未认识的人会被填成「未知用户xxxx」，这类值一律不用
        if person_name.startswith("未知用户"):
            person_name = ""

        if bool(self._opt("manual_alias", "use_manual_aliases", True)):
            await self._load_manual_aliases()
        alias = self._first_manual_alias(person_id)

        source = self._name_replace_source()
        if source == NAME_SOURCE_MANUAL_ALIAS:
            candidate = alias
        elif source == NAME_SOURCE_PERSON_NAME:
            candidate = person_name
        else:
            candidate = person_name if person_name and person_name.casefold() != key.casefold() else (alias or person_name)

        candidate = str(candidate or "").strip()
        if not candidate:
            self._log_debug(
                f"没有可用称呼: {platform}:{user_id} person_name={person_name!r} alias={alias!r}"
            )
            return ""
        if candidate.casefold() == key.casefold():
            self._log_debug(f"称呼与当前显示名相同，无需替换: {platform}:{user_id} name={candidate!r}")
            return ""
        return candidate

    # ------------------------------------------------------------ 人物查询

    async def _person_field(self, person_id: str, field_name: str) -> Any:
        """读取人物字段，并做短时缓存。"""

        cache_seconds = max(1, int(self._opt("injection", "cache_seconds", 60)))
        cached = self._person_cache.get(person_id)
        if cached is not None and time.time() - cached[0] < cache_seconds and field_name in cached[1]:
            return cached[1][field_name]

        value: Any = None
        try:
            reply = await self.ctx.person.get_value(person_id, field_name)
            if isinstance(reply, dict):
                if reply.get("success", True):
                    value = reply.get("value")
                else:
                    value = None
                    self._log_debug(
                        f"person.get_value 被宿主拒绝: person_id={person_id} "
                        f"field={field_name} error={reply.get('error')!r}"
                    )
            else:
                value = reply
        except Exception as exc:
            self._log_debug(f"读取人物字段异常: person_id={person_id} field={field_name} err={exc!r}")

        bucket = self._person_cache.get(person_id)
        payload = dict(bucket[1]) if bucket is not None else {}
        payload[field_name] = value
        self._person_cache[person_id] = (time.time(), payload)
        return value

    async def _resolve_person_id(self, person: dict[str, str]) -> str:
        """把 platform + user_id 解析成内部 person_id。

        身份只认 platform + user_id（QQ 号）：群名片/QQ 昵称由本人随意修改，
        按名称反查会认错对象，因此不做任何名称兜底。
        """

        platform = str(person.get("platform") or "").strip()
        user_id = str(person.get("user_id") or "").strip()

        if platform and user_id:
            try:
                reply = await self.ctx.person.get_id(platform, user_id)
                if isinstance(reply, dict) and reply.get("success", True):
                    person_id = str(reply.get("person_id") or "").strip()
                    if person_id:
                        return person_id
            except Exception as exc:
                self._log_debug(f"person.get_id 失败: {platform}:{user_id} err={exc}")

        return ""

    async def _describe_person(self, person: dict[str, str], seen: str = "") -> dict[str, Any] | None:
        """汇总单个人物的称呼与别名。

        返回的 ``name`` 是 **planner 实际可见的名字**（优先取换名后的名字
        ``seen``，未换名时是消息里的原始名）——注入块以它为主语，保证块内
        名字与 planner 在消息里看到的一致；``aliases`` 是除此之外的全部
        已知叫法（档案主称呼、档案昵称、各群群名片、人工别名）。

        传入 `person["person_id"]` 时直接使用，不再做任何反查。
        """

        fallback_name = str(person.get("name") or "").strip()
        person_id = str(person.get("person_id") or "").strip()
        if not person_id:
            person_id = await self._resolve_person_id(person)

        known = bool(person_id) and bool(await self._person_field(person_id, "is_known"))
        person_name = ""
        nickname = ""
        group_cards: list[str] = []
        manual: list[str] = []
        if known:
            person_name = str(await self._person_field(person_id, "person_name") or "").strip()
            if person_name.startswith("未知用户"):
                person_name = ""
            nickname = str(await self._person_field(person_id, "nickname") or "").strip()
            group_cards = self._extract_group_cardnames(await self._person_field(person_id, "group_cardname_list"))
            if bool(self._opt("manual_alias", "use_manual_aliases", True)):
                manual_map = await self._load_manual_aliases()
                manual = manual_map.get(person_id, [])

        # 主语：planner 实际可见名 > 档案主称呼 > 档案昵称 > 原始名 > person_id
        seen = str(seen or "").strip() or fallback_name
        name = seen or person_name or nickname or person_id
        if not name:
            return None

        aliases: list[str] = []
        seen_key = name.casefold()
        for candidate in (person_name, nickname, *group_cards, *manual):
            text = str(candidate or "").strip()
            key = text.casefold()
            if not text or key == seen_key:
                continue
            if key in {a.casefold() for a in aliases}:
                continue
            aliases.append(text)

        return {"person_id": person_id, "name": name, "aliases": aliases}

    @staticmethod
    def _entry_informative(entry: dict[str, Any], include_aliases: bool) -> bool:
        """判断条目是否值得注入。

        主语就是 planner 实际可见的名字，块内唯一能提供的新信息是"这个人
        还有哪些叫法"——没有其他叫法（或别名注入关闭）时，任何注入都是
        零增量。
        """

        if not include_aliases:
            return False
        name = str(entry.get("name") or "").strip()
        return bool(name) and bool(entry.get("aliases"))

    @staticmethod
    def _extract_group_cardnames(raw_value: Any) -> list[str]:
        """从 group_cardname_list 中提取群名片。"""

        names: list[str] = []
        if isinstance(raw_value, str):
            try:
                raw_value = json.loads(raw_value)
            except Exception:
                return names
        if not isinstance(raw_value, (list, tuple)):
            return names
        for item in raw_value:
            if isinstance(item, dict):
                names.append(str(item.get("group_cardname") or "").strip())
            else:
                names.append(str(item or "").strip())
        return names

    # -------------------------------------------------------- 人工别名读取

    async def _resolve_metadata_db(self) -> Path | None:
        """定位 A_Memorix 的 metadata.db。"""

        cached_path = self._manual_cache.get("path")
        if isinstance(cached_path, Path) and cached_path.is_file():
            return cached_path

        explicit = str(self._opt("manual_alias", "metadata_db_path", "") or "").strip()
        if explicit:
            path = Path(explicit).expanduser()
            if path.is_file():
                self._manual_cache["path"] = path
                return path
            self._log_warning(f"配置的 metadata_db_path 不存在: {explicit}")
            return None

        data_dir = ""
        try:
            value = await self.ctx.config.get("a_memorix.storage.data_dir", "")
            data_dir = str(value or "").strip()
        except Exception as exc:
            self._log_debug(f"读取 a_memorix.storage.data_dir 失败: {exc}")

        candidates: list[Path] = []
        if data_dir:
            candidates.append(Path(data_dir) / "metadata" / "metadata.db")
        candidates.append(Path("data") / "a-memorix" / "metadata" / "metadata.db")
        candidates.append(Path("data") / "metadata" / "metadata.db")

        for candidate in candidates:
            try:
                if candidate.is_file():
                    resolved = candidate.resolve()
                    self._manual_cache["path"] = resolved
                    return resolved
            except OSError:
                continue
        return None

    async def _load_manual_aliases(self) -> dict[str, list[str]]:
        """读取人工别名覆盖表，带短时缓存。"""

        cache_seconds = max(1, int(self._opt("manual_alias", "cache_seconds", 30)))
        cached = self._manual_cache.get("data")
        if isinstance(cached, dict) and time.time() - float(self._manual_cache.get("ts") or 0.0) < cache_seconds:
            return cached

        data: dict[str, list[str]] = {}
        path = await self._resolve_metadata_db()
        if path is not None:
            try:
                connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=1.5)
                try:
                    rows = connection.execute(
                        "SELECT person_id, aliases_json FROM person_profile_alias_overrides"
                    ).fetchall()
                finally:
                    connection.close()
                for person_id, raw_aliases in rows:
                    aliases = _coerce_alias_list(raw_aliases)
                    if aliases:
                        data[str(person_id)] = aliases
            except Exception as exc:
                self._log_debug(f"读取人工别名失败，已忽略: {exc}")

        self._manual_cache["ts"] = time.time()
        self._manual_cache["data"] = data
        return data

    # ------------------------------------------------------------ 注入实现

    def _build_reference_text(self, entries: list[dict[str, Any]]) -> str:
        """拼装内部参考文本。

        每行主语是 planner 实际可见的名字，后面列这个人其余的全部叫法；
        不再有恒等的"称呼=主语"字段。
        """

        title = str(self._opt("injection", "title", "") or "").strip() or "【人物称呼与别名-内部参考】"
        footer = str(self._opt("injection", "footer", "") or "").strip()

        lines = [title]
        for entry in entries:
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            aliases = [str(item) for item in entry.get("aliases") or [] if str(item).strip()]
            if aliases:
                lines.append(f"{name}：别名={'、'.join(aliases)}")
            else:
                lines.append(name)
        if len(lines) == 1:
            return ""
        if footer:
            lines.append(footer)
        return "\n".join(lines)

    @staticmethod
    def _build_reference_item(text: str) -> dict[str, Any]:
        """构造一条可注入 planner 请求的 UserMessageItem 快照。"""

        return {
            "item_type": "UserMessageItem",
            "meta": {
                "item_id": uuid.uuid4().hex,
                "logical_turn_id": None,
                "timestamp": datetime.now().isoformat(),
            },
            "parts": [{"type": "text", "text": text}],
        }

    @staticmethod
    def _insert_reference_item(items: list[Any], text: str) -> list[Any] | None:
        """把参考消息插到最近一条 user 消息之前。"""

        if not items:
            return None

        insert_at = len(items)
        for index in range(len(items) - 1, -1, -1):
            item = items[index]
            if isinstance(item, dict) and str(item.get("item_type") or "") == "UserMessageItem":
                insert_at = index
                break

        return [*items[:insert_at], PersonNameAliasPlugin._build_reference_item(text), *items[insert_at:]]

    def _name_replace_map(self, session_id: str) -> dict[str, str]:
        """汇总「旧称呼 -> 新称呼」映射，供 planner 范围换名使用。"""

        self._prune_recent()
        buckets: list[dict[str, Any]] = []
        exact = self._recent.get(session_id)
        if isinstance(exact, dict):
            buckets.append(exact)
        buckets.extend(item for key, item in self._recent.items() if key != session_id and isinstance(item, dict))

        mapping: dict[str, str] = {}
        for bucket in buckets:
            for person in bucket.get("people") or []:
                if not isinstance(person, dict):
                    continue
                old_name = str(person.get("name") or "").strip()
                new_name = str(person.get("replace") or "").strip()
                if old_name and new_name and old_name != new_name and old_name not in mapping:
                    mapping[old_name] = new_name
        return mapping

    @staticmethod
    def _patch_planner_text(text: str, mapping: dict[str, str]) -> str:
        """改写请求文本里的 user/group_card 属性、[称呼] 前缀与 @称呼。"""

        def sub_attr(match: "re.Match[str]") -> str:
            attr, value = match.group(1), match.group(2)
            new_name = mapping.get(value.strip())
            return f'{attr}="{new_name}"' if new_name else match.group(0)

        patched = re.sub(r'(user|group_card)="([^"]*)"', sub_attr, text)
        for old_name, new_name in mapping.items():
            patched = patched.replace(f"[{old_name}]", f"[{new_name}]")
            patched = patched.replace(f"@{old_name}", f"@{new_name}")
            patched = patched.replace(f"回复了{old_name}的消息", f"回复了{new_name}的消息")
        return patched

    @classmethod
    def _rewrite_item_names(cls, items: list[Any], mapping: dict[str, str]) -> tuple[list[Any], bool]:
        """把 planner 请求条目文本里的旧称呼换成新称呼（不改原对象）。"""

        if not mapping:
            return items, False

        changed = False
        rewritten_items: list[Any] = []
        for item in items:
            parts = item.get("parts") if isinstance(item, dict) else None
            if not isinstance(parts, list):
                rewritten_items.append(item)
                continue

            item_changed = False
            new_parts: list[Any] = []
            for part in parts:
                text = part.get("text") if isinstance(part, dict) else None
                if not isinstance(text, str) or not text:
                    new_parts.append(part)
                    continue
                patched = cls._patch_planner_text(text, mapping)
                if patched == text:
                    new_parts.append(part)
                    continue
                item_changed = True
                cloned_part = dict(part)
                cloned_part["text"] = patched
                new_parts.append(cloned_part)

            if item_changed:
                cloned_item = dict(item)
                cloned_item["parts"] = new_parts
                rewritten_items.append(cloned_item)
                changed = True
            else:
                rewritten_items.append(item)

        return (rewritten_items if changed else items), changed

    @HookHandler(
        "maisaka.planner.before_request",
        name="inject_name_alias",
        description="在 planner 请求中注入人物称呼与别名（planner 范围换名也在这里）",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_planner_before_request(self, **kwargs: Any) -> dict[str, Any]:
        """向 planner 请求插入人物称呼与别名，必要时改写请求里的称呼。"""

        try:
            if not bool(self._opt("plugin", "enabled", True)):
                return {"action": "continue"}

            items = kwargs.get("items")
            if not isinstance(items, list) or not items:
                return {"action": "continue"}

            session_id = str(kwargs.get("session_id") or "").strip()
            working_items: list[Any] = list(items)
            changed = False

            # 1) planner 范围换名：先把请求文本里的旧称呼改掉（不碰消息体与数据库）
            if self._name_replace_enabled() and self._name_replace_scope() == SCOPE_PLANNER:
                rename_map = self._name_replace_map(session_id)
                working_items, renamed = self._rewrite_item_names(working_items, rename_map)
                changed = changed or renamed
                if renamed and bool(self._opt("debug", "log_replace", False)):
                    self._log_info(f"已在 planner 请求中替换称呼: {rename_map}")

            # 2) 注入人物称呼与别名内部参考（injection.enabled 独立开关，与昵称替换解耦）
            if bool(self._opt("injection", "enabled", True)):
                include_aliases = bool(self._opt("injection", "include_aliases", True))
                replace_enabled = self._name_replace_enabled()
                people = self._pick_people(session_id)
                if people:
                    max_people = max(1, min(MAX_RECORDED_PEOPLE, int(self._opt("injection", "max_people", 3))))
                    entries: list[dict[str, Any]] = []
                    for person in people[:max_people]:
                        # 主语 = planner 实际可见名：换名生效用换名后的名字，否则用原始名
                        replace_value = str(person.get("replace") or "").strip()
                        seen = replace_value if (replace_enabled and replace_value) else str(
                            person.get("name") or ""
                        ).strip()
                        entry = await self._describe_person(person, seen=seen)
                        if entry is not None and self._entry_informative(entry, include_aliases):
                            entries.append(entry)

                    text = self._build_reference_text(entries)
                    if text:
                        new_items = self._insert_reference_item(working_items, text)
                        if new_items is not None:
                            working_items = new_items
                            changed = True
                            if bool(self._opt("debug", "log_injection", False)):
                                self._log_info(f"已注入人物称呼与别名（{len(entries)} 人）:\n{text}")

            if not changed:
                return {"action": "continue"}
            return {"action": "continue", "modified_kwargs": {**kwargs, "items": working_items}}
        except Exception as exc:
            self._log_warning(f"注入人物称呼与别名失败，已跳过: {exc}")
            return {"action": "continue"}

def create_plugin() -> PersonNameAliasPlugin:
    """创建插件实例。"""

    return PersonNameAliasPlugin()
