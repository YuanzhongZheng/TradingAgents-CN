"""工具导出接口。

按需导入：导入包本身不会加载工具实现；全量发现请用 ToolRegistry.list_all()。
"""

from core.tools._lazy import resolve_export

_EXPORTS = {
    'bind_agent_tools_and_validate_candidate': ('.nanobot_agent_builder_tools', 'bind_agent_tools_and_validate_candidate'),
    'create_or_update_runtime_agent_config': ('.nanobot_agent_builder_tools', 'create_or_update_runtime_agent_config'),
    'create_or_update_universal_agent_prompt_template': ('.nanobot_agent_builder_tools', 'create_or_update_universal_agent_prompt_template'),
    'generate_confirmed_candidate_agent': ('.nanobot_agent_builder_tools', 'generate_confirmed_candidate_agent'),
    'get_current_thread_effective_prompt_template': ('.nanobot_agent_builder_tools', 'get_current_thread_effective_prompt_template'),
    'get_effective_prompt_template': ('.nanobot_agent_builder_tools', 'get_effective_prompt_template'),
    'get_prompt_template_detail': ('.nanobot_agent_builder_tools', 'get_prompt_template_detail'),
    'get_agent_blueprint_details': ('.nanobot_agent_builder_tools', 'get_agent_blueprint_details'),
    'inspect_agent_blueprints': ('.nanobot_agent_builder_tools', 'inspect_agent_blueprints'),
    'inspect_agent_workshop_assets': ('.nanobot_agent_builder_tools', 'inspect_agent_workshop_assets'),
    'inspect_prompt_templates': ('.nanobot_agent_builder_tools', 'inspect_prompt_templates'),
    'inspect_runtime_agent_configs': ('.nanobot_agent_builder_tools', 'inspect_runtime_agent_configs'),
    'prepare_agent_requirement_intake': ('.nanobot_agent_builder_tools', 'prepare_agent_requirement_intake'),
    'prepare_agent_generation_confirmation': ('.nanobot_agent_builder_tools', 'prepare_agent_generation_confirmation'),
    'resolve_agent_workshop_blocking_gaps': ('.nanobot_agent_builder_tools', 'resolve_agent_workshop_blocking_gaps'),
    'start_agent_workshop_gap_resolution': ('.nanobot_agent_builder_tools', 'start_agent_workshop_gap_resolution'),
    'inspect_agent_workshop_gap_resolution': ('.nanobot_agent_builder_tools', 'inspect_agent_workshop_gap_resolution'),
    'cancel_agent_workshop_gap_resolution': ('.nanobot_agent_builder_tools', 'cancel_agent_workshop_gap_resolution'),
    'retry_agent_workshop_gap_resolution_item': ('.nanobot_agent_builder_tools', 'retry_agent_workshop_gap_resolution_item'),
    'search_project_capabilities': ('.nanobot_agent_builder_tools', 'search_project_capabilities'),
}

__all__ = [
    'search_project_capabilities',
    'inspect_runtime_agent_configs',
    'inspect_agent_blueprints',
    'get_agent_blueprint_details',
    'inspect_agent_workshop_assets',
    'inspect_prompt_templates',
    'get_prompt_template_detail',
    'get_current_thread_effective_prompt_template',
    'get_effective_prompt_template',
    'prepare_agent_requirement_intake',
    'prepare_agent_generation_confirmation',
    'create_or_update_runtime_agent_config',
    'create_or_update_universal_agent_prompt_template',
    'bind_agent_tools_and_validate_candidate',
    'generate_confirmed_candidate_agent',
    'resolve_agent_workshop_blocking_gaps',
    'start_agent_workshop_gap_resolution',
    'inspect_agent_workshop_gap_resolution',
    'cancel_agent_workshop_gap_resolution',
    'retry_agent_workshop_gap_resolution_item',
]


def __getattr__(name):
    return resolve_export(name, _EXPORTS, globals())


def __dir__():
    return sorted(set(globals()) | set(_EXPORTS))
