"""Fresh interpreters catch package import side effects hidden by pytest collection."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap

ROOT = Path(__file__).resolve().parents[2]


def run_python(code):
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)], cwd=ROOT,
        env={**os.environ, "USE_MONGODB_STORAGE": "false"},
        text=True, capture_output=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_pure_algorithms_do_not_initialize_registry_or_sdks():
    run_python("""
        import sys
        from core.tools.implementations.technical.chanlun import analyze_chanlun_bars
        from core.tools.implementations.technical.elliott_wave import analyze_elliott_bars
        for name in ('core.tools.registry', 'pandas', 'numpy', 'langchain_core',
                     'akshare', 'baostock', 'xtquant'):
            assert name not in sys.modules, name
    """)


def test_targeted_lookup_concurrent_initialization_and_public_exports():
    run_python("""
        from concurrent.futures import ThreadPoolExecutor
        import sys
        from core.tools import ToolRegistry, get_tool_registry
        with ThreadPoolExecutor(8) as pool:
            registries = list(pool.map(lambda _: ToolRegistry(), range(32)))
        registry = get_tool_registry()
        assert all(r is registry for r in registries)
        assert registry._functions == {}
        with ThreadPoolExecutor(8) as pool:
            functions = list(pool.map(lambda _: registry.get_function('analyze_chanlun'), range(32)))
        assert all(f is functions[0] and callable(f) for f in functions)
        assert set(registry._functions) == {'analyze_chanlun', 'analyze_elliott_wave'}
        assert 'core.tools.implementations.legacy_bridge' not in sys.modules
        assert 'core.tools.implementations.assistant_ops.agent_executor' not in sys.modules
        from core.tools.implementations import analyze_chanlun
        assert analyze_chanlun.func is functions[0]
        metadata = registry.get('analyze_chanlun')
        assert metadata.parameters
        assert registry.get_langchain_tool('analyze_chanlun').name == 'analyze_chanlun'
        assert registry.get_by_ids(['analyze_chanlun'])[0] is metadata
        registry.unregister('analyze_chanlun')
        assert registry.get_function('analyze_chanlun') is None
        assert not registry.has_tool('analyze_chanlun')
        registry.register_function('analyze_chanlun', lambda: 'replacement', 'Replacement', 'technical')
        assert registry.get_function('analyze_chanlun')() == 'replacement'
        registry.unregister('get_vwap')
        registry.list_all()
        assert registry.get_function('get_vwap') is None
    """)


def test_full_discovery_retains_legacy_assistant_and_extension_tools():
    run_python("""
        import importlib
        from concurrent.futures import ThreadPoolExecutor
        from core.tools import get_tool_registry
        from core.tools._tool_index import TOOL_MODULES
        from core.tools.config import BUILTIN_TOOLS
        registry = get_tool_registry()
        before = {key: value.model_dump() for key, value in BUILTIN_TOOLS.items()}
        with ThreadPoolExecutor(3) as pool:
            listing = pool.submit(registry.list_all)
            lookup = pool.submit(registry.get_function, 'get_yfin_data_legacy')
            second_listing = pool.submit(registry.list_all)
            tools = {tool.id: tool for tool in listing.result()}
            assert lookup.result()
            assert {tool.id for tool in second_listing.result()} == tools.keys()
        assert set(TOOL_MODULES) <= tools.keys(), sorted(set(TOOL_MODULES) - tools.keys())
        assert all(registry.get_function(key) for key in TOOL_MODULES)
        assert registry.get_function('get_yfin_data_legacy')
        assert registry.get_by_category('assistant_ops')
        assert registry.get_online_tools() and registry.get_offline_tools()
        assert before == {key: value.model_dump() for key, value in BUILTIN_TOOLS.items()}
        assert registry.get_function('unknown_tool_test') is None
        registry.register_function('custom_test', lambda: 'ok', 'Custom', 'utility')
        assert registry.get_function('custom_test')() == 'ok'
        assert registry.get('custom_test').name == 'Custom'
        for suffix in ('', '.implementations', '.implementations.technical',
                       '.implementations.market', '.implementations.assistant_ops'):
            package = importlib.import_module('core.tools' + suffix)
            for name in package.__all__:
                module_name, attribute = package._EXPORTS[name]
                module = importlib.import_module(module_name, package.__name__)
                expected = getattr(module, attribute) if attribute else module
                assert getattr(package, name) is expected, name
            assert not hasattr(package, 'nonexistent_export_test')
    """)


def test_index_matches_decorators():
    result = subprocess.run([sys.executable, "scripts/generate_tool_index.py", "--check"],
                            cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_indexed_lookup_works_without_source_files():
    run_python("""
        from pathlib import Path
        import py_compile
        import tempfile
        from core.tools import get_tool_registry
        from core.tools._tool_index import TOOL_MODULES
        import core.tools.implementations as package
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bytecode_fixture.py'
            path.write_text(
                "from core.tools.base import register_tool\\n"
                "@register_tool(tool_id='bytecode_fixture', category='utility')\\n"
                "def execute(): return 'bytecode-only'\\n"
            )
            py_compile.compile(str(path), cfile=str(path.with_suffix('.pyc')), doraise=True)
            path.unlink()
            package.__path__.append(directory)
            TOOL_MODULES['bytecode_fixture'] = 'core.tools.implementations.bytecode_fixture'
            assert get_tool_registry().get_function('bytecode_fixture')() == 'bytecode-only'
    """)
