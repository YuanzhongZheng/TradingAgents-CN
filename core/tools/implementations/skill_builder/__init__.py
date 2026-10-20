"""工具导出接口。

按需导入：导入包本身不会加载工具实现；全量发现请用 ToolRegistry.list_all()。
"""

from core.tools._lazy import resolve_export

_EXPORTS = {
    'generate_confirmed_external_skill': ('.nanobot_skill_builder_tools', 'generate_confirmed_external_skill'),
    'inspect_external_skill_generation_session': ('.nanobot_skill_builder_tools', 'inspect_external_skill_generation_session'),
    'preview_external_skill_spec': ('.nanobot_skill_builder_tools', 'preview_external_skill_spec'),
    'respond_to_external_skill_generation_session': ('.nanobot_skill_builder_tools', 'respond_to_external_skill_generation_session'),
    'start_external_skill_generation_session': ('.nanobot_skill_builder_tools', 'start_external_skill_generation_session'),
}

__all__ = [
    'start_external_skill_generation_session',
    'respond_to_external_skill_generation_session',
    'preview_external_skill_spec',
    'generate_confirmed_external_skill',
    'inspect_external_skill_generation_session',
]


def __getattr__(name):
    return resolve_export(name, _EXPORTS, globals())


def __dir__():
    return sorted(set(globals()) | set(_EXPORTS))
