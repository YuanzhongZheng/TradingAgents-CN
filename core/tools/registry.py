"""
工具注册表

管理所有可用工具的元数据和发现
"""

import logging
import threading
from typing import Dict, List, Optional, Callable, Any, Tuple, Type

from .config import ToolMetadata, ToolCategory, BUILTIN_TOOLS

logger = logging.getLogger(__name__)


_PARAMETER_TYPE_MAP: Dict[str, Tuple[type, Any]] = {
    "string": (str, ""),
    "number": (float, 0.0),
    "integer": (int, 0),
    "boolean": (bool, False),
    "array": (list, []),
    "object": (dict, {}),
}


class ToolRegistry:
    """
    工具注册表（单例模式）
    
    用法:
        registry = ToolRegistry()
        
        # 获取所有工具
        all_tools = registry.list_all()
        
        # 获取特定类别工具
        market_tools = registry.get_by_category("market")
        
        # 获取某个 Agent 的可用工具
        tools = registry.get_tools_for_agent("market_analyst")
    """
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super(ToolRegistry, cls).__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        with self._lock:
            if self._initialized:
                return
            self._tools: Dict[str, ToolMetadata] = {}
            self._functions: Dict[str, Callable] = {}  # 存储实际函数实现
            self._discovery_lock = threading.RLock()
            self._discovered = False
            self._discovering = False
            self._disabled = set()
            self._load_builtin_tools()
            self._initialized = True

    def _load_builtin_tools(self) -> None:
        """加载内置工具元数据"""
        for tool_id, metadata in BUILTIN_TOOLS.items():
            self._tools[tool_id] = metadata.model_copy(deep=True)

    def _load_tool_implementations(self) -> None:
        """显式全量发现；定向查询不经过此路径。"""
        with self._discovery_lock:
            if self._discovered or self._discovering:
                return
            self._discovering = True
            try:
                from .loader import get_tool_loader
                get_tool_loader().load_all()
                self._discovered = True
            finally:
                self._discovering = False

    def _ensure_tool(self, tool_id: str) -> None:
        if tool_id in self._disabled or tool_id in self._functions:
            return
        from ._tool_index import TOOL_MODULES
        if tool_id in TOOL_MODULES:
            from .loader import get_tool_loader
            get_tool_loader().load_tool(tool_id)
        elif tool_id not in self._tools:
            # 保留动态扩展工具的发现能力，未知 ID 仅首次触发全量扫描。
            self._load_tool_implementations()
    
    def register(self, metadata: ToolMetadata, override: bool = False) -> None:
        """注册工具"""
        if metadata.id in self._tools and not override:
            raise ValueError(f"工具 '{metadata.id}' 已注册，使用 override=True 覆盖")
        self._tools[metadata.id] = metadata
        self._disabled.discard(metadata.id)
    
    def unregister(self, tool_id: str) -> None:
        """注销工具"""
        self._tools.pop(tool_id, None)
        self._functions.pop(tool_id, None)
        self._disabled.add(tool_id)
    
    def get(self, tool_id: str) -> Optional[ToolMetadata]:
        """获取工具元数据"""
        self._ensure_tool(tool_id)
        return self._tools.get(tool_id)

    def has_tool(self, tool_id: str) -> bool:
        """检查工具是否已注册"""
        return self.get(tool_id) is not None

    def get_tool(self, tool_id: str) -> Optional[ToolMetadata]:
        """获取工具元数据（别名方法）"""
        return self.get(tool_id)

    def get_all_tools(self) -> List[ToolMetadata]:
        """获取所有工具（别名方法）"""
        return self.list_all()

    def list_all(self) -> List[ToolMetadata]:
        """列出所有工具"""
        self._load_tool_implementations()
        return list(self._tools.values())
    
    def get_by_category(self, category: str) -> List[ToolMetadata]:
        """按类别获取工具"""
        return [t for t in self.list_all() if t.category == category]
    
    def get_by_ids(self, tool_ids: List[str]) -> List[ToolMetadata]:
        """根据 ID 列表获取工具"""
        return [tool for tid in tool_ids if (tool := self.get(tid)) is not None]
    
    def get_online_tools(self) -> List[ToolMetadata]:
        """获取所有在线工具"""
        return [t for t in self.list_all() if t.is_online]
    
    def get_offline_tools(self) -> List[ToolMetadata]:
        """获取所有离线工具"""
        return [t for t in self.list_all() if not t.is_online]

    # ==================== 函数注册（新增） ====================

    def register_function(
        self,
        tool_id: str,
        func: Callable,
        name: str,
        category: str,
        description: str = "",
        is_online: bool = True,
        is_legacy: bool = False,
        override: bool = False,
        parameters: Optional[List] = None,
        timeout_tier: str = "",
        _auto_discovered: bool = False,
        **kwargs
    ) -> None:
        """
        注册一个函数作为工具

        Args:
            tool_id: 工具唯一ID
            func: 实际的函数实现
            name: 工具显示名称
            category: 工具分类
            description: 工具描述
            is_online: 是否需要在线
            is_legacy: 是否为旧工具适配
            override: 是否覆盖已有工具
            parameters: 参数定义列表（可选，如果提供则覆盖元数据中的参数）
            timeout_tier: 超时分层 light/medium/heavy
        """
        # 防御性：tool_id 必须符合 OpenAI 工具名规范（^[a-zA-Z0-9_-]+$）
        from .fc_converter import sanitize_function_name
        sanitized = sanitize_function_name(tool_id)
        if sanitized != tool_id:
            logger.warning(f"⚠️ tool_id '{tool_id}' 含非法字符，已规范化为 '{sanitized}'")
            tool_id = sanitized
        # 延迟装饰器不能复活已注销工具；显式注册仍可重新启用它。
        if tool_id in self._disabled and _auto_discovered and not override:
            return
        self._disabled.discard(tool_id)

        if tool_id in self._tools:
            if tool_id not in self._functions:
                self._functions[tool_id] = func
                if parameters is not None:
                    self._tools[tool_id].parameters = parameters
                if timeout_tier:
                    self._tools[tool_id].timeout_tier = timeout_tier
                metadata_fields = getattr(ToolMetadata, "model_fields", {})
                for key, value in kwargs.items():
                    if key in metadata_fields:
                        setattr(self._tools[tool_id], key, value)
                logger.debug(f"为已注册工具添加函数实现: {tool_id}")
                return
            elif not override:
                logger.warning(f"工具 '{tool_id}' 已注册，跳过")
                return

        from .config import ToolTimeoutTier
        effective_tier = timeout_tier or ToolTimeoutTier.MEDIUM

        metadata_fields = getattr(ToolMetadata, "model_fields", {})
        metadata_payload = {
            "id": tool_id,
            "name": name,
            "description": description,
            "category": category,
            "is_online": is_online,
            "parameters": parameters or [],
            "timeout_tier": effective_tier,
        }
        for key, value in kwargs.items():
            if key in metadata_fields:
                metadata_payload[key] = value

        metadata = ToolMetadata(
            **metadata_payload,
        )

        self._tools[tool_id] = metadata
        self._functions[tool_id] = func

        logger.debug(f"注册工具函数: {tool_id} (legacy={is_legacy}, tier={effective_tier})")

    def get_function(self, tool_id: str) -> Optional[Callable]:
        """获取工具的函数实现"""
        self._ensure_tool(tool_id)
        return self._functions.get(tool_id)

    def _build_langchain_args_schema(self, metadata: ToolMetadata) -> Optional[Type]:
        """基于工具元数据构建 LangChain/Pydantic 参数模型。"""
        if not metadata.parameters:
            return None

        from pydantic import Field, create_model

        field_definitions = {}
        for parameter in metadata.parameters:
            python_type, fallback_default = _PARAMETER_TYPE_MAP.get(parameter.type, (Any, None))
            if parameter.required:
                default_value = ...
            else:
                default_value = parameter.default if parameter.default is not None else fallback_default

            field_definitions[parameter.name] = (
                python_type,
                Field(default_value, description=parameter.description),
            )

        model_name = f"{metadata.id.title().replace('_', '')}Args"
        return create_model(model_name, **field_definitions)

    def get_langchain_tool(self, tool_id: str):
        """
        获取 LangChain 格式的工具

        Returns:
            LangChain Tool 对象，如果工具未注册或无函数实现则返回 None
        """
        func = self.get_function(tool_id)
        if tool_id not in self._tools:
            return None

        if func is None:
            return None

        # 如果已经是 LangChain tool，直接返回
        if hasattr(func, 'invoke') or hasattr(func, 'run'):
            return func

        # 🔥 检查函数是否为异步函数，若是则自动包装为同步函数
        import asyncio
        import inspect
        import functools

        if inspect.iscoroutinefunction(func):
            @functools.wraps(func)
            def _sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop is not None and loop.is_running():
                    import concurrent.futures
                    future = asyncio.run_coroutine_threadsafe(func(*args, **kwargs), loop)
                    return future.result(timeout=120)
                return asyncio.run(func(*args, **kwargs))

            func = _sync_wrapper
            logger.info(
                f"✅ 工具 {tool_id} 的异步函数已自动包装为同步函数，LangChain 可正常调用。"
            )
        
        # 否则包装为 LangChain tool
        from langchain_core.tools import StructuredTool

        metadata = self._tools[tool_id]

        args_schema = None
        if metadata.source == "generated" and metadata.parameters:
            args_schema = self._build_langchain_args_schema(metadata)

        # 使用 StructuredTool.from_function 来创建工具
        # 这样可以显式指定 name、description 和 args_schema
        lc_tool = StructuredTool.from_function(
            func=func,
            name=tool_id,
            description=metadata.description or func.__doc__ or "No description available",
            args_schema=args_schema,
        )

        return lc_tool

    def get_langchain_tools(self, tool_ids: List[str]) -> List:
        """批量获取 LangChain 格式的工具"""
        tools = []
        for tool_id in tool_ids:
            tool = self.get_langchain_tool(tool_id)
            if tool:
                tools.append(tool)
        return tools

    def register_external_skill(
        self,
        tool_id: str,
        func: Callable,
        display_name: str,
        description: str,
        category: str = "external",
        parameters: Optional[List] = None,
        **metadata_kwargs: Any,
    ) -> None:
        """
        注册 AI 生成的外部 Skill 到工具注册表。

        生成的 Skill 通过沙箱包装后注册为可被 Agent 调用的工具。
        """
        self.register_function(
            tool_id=tool_id,
            func=func,
            name=display_name,
            category=category,
            description=description,
            is_online=True,
            override=True,
            parameters=parameters,
            **metadata_kwargs,
        )
        if tool_id in self._tools:
            self._tools[tool_id].source = "generated"
        logger.info(f"注册外部 Skill: {tool_id}")

    def get_external_skills(self) -> List:
        """获取所有 AI 生成的外部 Skill"""
        return [t for t in self._tools.values() if getattr(t, 'source', '') == 'generated']


# 全局注册表实例
_global_registry: Optional[ToolRegistry] = None


def get_tool_registry() -> ToolRegistry:
    """获取全局工具注册表"""
    global _global_registry
    if _global_registry is None:
        _global_registry = ToolRegistry()
    return _global_registry

