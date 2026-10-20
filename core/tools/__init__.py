"""工具系统模块

提供工具注册、发现和管理功能

按需导入：导入包本身不会加载工具实现；全量发现请用 ToolRegistry.list_all()。
"""

from core.tools._lazy import resolve_export

_EXPORTS = {
    'ToolRegistry': ('.registry', 'ToolRegistry'),
    'get_tool_registry': ('.registry', 'get_tool_registry'),
    'ToolMetadata': ('.config', 'ToolMetadata'),
    'ToolCategory': ('.config', 'ToolCategory'),
    'ToolTimeoutTier': ('.config', 'ToolTimeoutTier'),
    'TOOL_TIMEOUT_SECONDS': ('.config', 'TOOL_TIMEOUT_SECONDS'),
    'BUILTIN_TOOLS': ('.config', 'BUILTIN_TOOLS'),
    'BaseTool': ('.base', 'BaseTool'),
    'register_tool': ('.base', 'register_tool'),
    'ToolLoader': ('.loader', 'ToolLoader'),
    'get_tool_loader': ('.loader', 'get_tool_loader'),
    'tool_metadata_to_openai': ('.fc_converter', 'tool_metadata_to_openai'),
    'tool_metadata_list_to_openai': ('.fc_converter', 'tool_metadata_list_to_openai'),
}

__all__ = [
    'ToolRegistry',
    'get_tool_registry',
    'ToolMetadata',
    'ToolCategory',
    'ToolTimeoutTier',
    'TOOL_TIMEOUT_SECONDS',
    'BUILTIN_TOOLS',
    'BaseTool',
    'register_tool',
    'ToolLoader',
    'get_tool_loader',
    'tool_metadata_to_openai',
    'tool_metadata_list_to_openai',
]


def __getattr__(name):
    return resolve_export(name, _EXPORTS, globals())


def __dir__():
    return sorted(set(globals()) | set(_EXPORTS))
