"""
工具加载器

动态加载工具模块
"""

import logging
import importlib
import os
import sys
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


class ToolLoader:
    """
    工具加载器
    
    自动发现和加载工具模块
    
    用法:
        loader = ToolLoader()
        
        # 加载所有工具
        loader.load_all()
        
        # 加载特定类别
        loader.load_category("market")
        
        # 加载特定工具
        loader.load_tool("get_stock_market_data_unified")
    """
    
    def __init__(self, tools_dir: Optional[str] = None):
        """
        初始化工具加载器
        
        Args:
            tools_dir: 工具目录路径，默认为 core/tools/implementations
        """
        if tools_dir is None:
            # 默认工具目录
            current_dir = Path(__file__).parent
            tools_dir = current_dir / "implementations"
        
        self.tools_dir = Path(tools_dir)
        self._loaded_modules = set()
    
    def load_all(self) -> int:
        """
        加载所有工具模块
        
        Returns:
            加载的模块数量
        """
        if not self.tools_dir.exists():
            logger.warning(f"工具目录不存在: {self.tools_dir}")
            return 0
        
        count = 0

        # 遍历所有 Python 文件（含 .pyc：京东云源码保护后 .py 被删，只剩 .pyc）
        py_files = list(self.tools_dir.rglob("*.py"))
        py_files += list(self.tools_dir.rglob("*.pyc"))
        for py_file in py_files:
            if py_file.name.startswith("_"):
                continue
            # __pycache__ 里的 .pyc 跳过（由同名 .py 或 .pyc 顶层文件覆盖）
            if "__pycache__" in py_file.parts:
                continue

            try:
                self._load_module_from_file(py_file)
                count += 1
            except Exception as e:
                logger.error(f"加载工具模块失败 {py_file}: {e}")
        
        logger.info(f"✅ 加载了 {count} 个工具模块")
        return count
    
    def load_category(self, category: str) -> int:
        """
        加载特定类别的工具
        
        Args:
            category: 工具类别（对应子目录名）
            
        Returns:
            加载的模块数量
        """
        category_dir = self.tools_dir / category
        
        if not category_dir.exists():
            logger.warning(f"类别目录不存在: {category_dir}")
            return 0
        
        count = 0
        
        for py_file in category_dir.glob("*.py"):
            if py_file.name.startswith("_"):
                continue
            
            try:
                self._load_module_from_file(py_file)
                count += 1
            except Exception as e:
                logger.error(f"加载工具模块失败 {py_file}: {e}")
        
        logger.info(f"✅ 加载了 {count} 个 {category} 类别的工具")
        return count
    
    def load_tool(self, tool_id: str) -> bool:
        """
        加载特定工具
        
        Args:
            tool_id: 工具ID
            
        Returns:
            是否加载成功
        """
        # 一个模块可以注册多个工具，文件名不一定与工具 ID 一致。
        from ._tool_index import TOOL_MODULES
        module_name = TOOL_MODULES.get(tool_id)
        if module_name and self.tools_dir.resolve() == Path(__file__).with_name("implementations").resolve():
            try:
                # importlib 自身的模块锁保证并发调用等待装饰器注册完成。
                importlib.import_module(module_name)
                self._loaded_modules.add(module_name)
                return True
            except Exception as exc:
                logger.warning("加载工具失败 %s: %s", tool_id, exc)
                return False

        if not self.tools_dir.exists():
            return False
        # 非内置目录保留原有动态查找路径。
        for category_dir in self.tools_dir.iterdir():
            if not category_dir.is_dir():
                continue
            
            # 尝试 tool_id.py
            tool_file = category_dir / f"{tool_id}.py"
            if tool_file.exists():
                try:
                    self._load_module_from_file(tool_file)
                    logger.info(f"✅ 加载工具: {tool_id}")
                    return True
                except Exception as e:
                    logger.error(f"加载工具失败 {tool_id}: {e}")
                    return False
        
        logger.warning(f"找不到工具: {tool_id}")
        return False
    
    def _load_module_from_file(self, file_path: Path) -> None:
        """
        从文件加载模块

        Args:
            file_path: Python 文件路径
        """
        # 计算模块名：从项目根目录开始
        # 例如：C:\TradingAgentsCN\core\tools\implementations\market\stock_market_data.py
        # 应该变成：core.tools.implementations.market.stock_market_data

        # 获取项目根目录（假设是当前工作目录或者向上查找）
        current_dir = Path.cwd()

        try:
            rel_path = file_path.relative_to(current_dir)
        except ValueError:
            # 如果文件不在当前目录下，尝试使用绝对路径的后几段
            # 从 core 开始
            parts = file_path.parts
            if 'core' in parts:
                core_index = parts.index('core')
                rel_path = Path(*parts[core_index:])
            else:
                raise ValueError(f"无法计算模块路径: {file_path}")

        module_name = str(rel_path.with_suffix("")).replace(os.sep, ".")

        # 避免重复加载（local tracker）
        if module_name in self._loaded_modules:
            return

        # 即使存在于 sys.modules，也通过 importlib 等待其他线程完成模块初始化。
        # 普通已完成导入不会重复执行模块。
        try:
            importlib.import_module(module_name)
        except RecursionError:
            # 循环引用：模块可能在另一个 import 链中正在加载，先记录下来
            logger.warning(f"模块 {module_name} 因循环引用跳过（可能已由 __init__.py 加载）")
            if module_name not in sys.modules:
                logger.error(f"模块 {module_name} 未加载成功，存在真实的循环依赖问题")
            else:
                self._loaded_modules.add(module_name)
            return

        self._loaded_modules.add(module_name)

        logger.debug(f"加载模块: {module_name}")
    
    def get_loaded_modules(self) -> List[str]:
        """获取已加载的模块列表"""
        return list(self._loaded_modules)


# 全局加载器实例
_global_loader: Optional[ToolLoader] = None


def get_tool_loader() -> ToolLoader:
    """获取全局工具加载器"""
    global _global_loader
    if _global_loader is None:
        _global_loader = ToolLoader()
    return _global_loader

