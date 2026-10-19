"""
股票分析服务。

负责把分析任务写入队列。旧版 TradingAgentsGraph 执行代码已删除。
"""

import asyncio
import uuid
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional, Callable
from pathlib import Path
import sys

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

# 初始化TradingAgents日志系统
from tradingagents.utils.logging_init import init_logging
init_logging()

from app.models.analysis import (
    AnalysisParameters, AnalysisResult, AnalysisTask, AnalysisBatch,
    AnalysisStatus, BatchStatus, SingleAnalysisRequest, BatchAnalysisRequest
)
from app.models.user import PyObjectId
from bson import ObjectId
from app.core.database import get_mongo_db
from app.core.redis_client import get_redis_service, RedisKeys
from app.services.queue_service import QueueService
from app.core.database import get_redis_client
from app.services.redis_progress_tracker import RedisProgressTracker
from app.services.config_provider import provider as config_provider
from app.services.queue import DEFAULT_USER_CONCURRENT_LIMIT, GLOBAL_CONCURRENT_LIMIT, VISIBILITY_TIMEOUT_SECONDS
from app.services.usage_statistics_service import UsageStatisticsService
from app.models.config import UsageRecord

import logging
logger = logging.getLogger(__name__)


class AnalysisService:
    """股票分析服务类"""

    def __init__(self):
        # 获取Redis客户端
        redis_client = get_redis_client()
        self.queue_service = QueueService(redis_client)
        # 初始化使用统计服务
        self.usage_service = UsageStatisticsService()
        self._trading_graph_cache = {}
        # 进度跟踪器缓存
        self._progress_trackers: Dict[str, RedisProgressTracker] = {}

    def _convert_user_id(self, user_id: str) -> PyObjectId:
        """将字符串用户ID转换为PyObjectId"""
        try:
            logger.info(f"🔄 开始转换用户ID: {user_id} (类型: {type(user_id)})")

            # 如果是admin用户，使用固定的ObjectId
            if user_id == "admin":
                # 使用固定的ObjectId作为admin用户ID
                admin_object_id = ObjectId("507f1f77bcf86cd799439011")
                logger.info(f"🔄 转换admin用户ID: {user_id} -> {admin_object_id}")
                return PyObjectId(admin_object_id)
            else:
                # 尝试将字符串转换为ObjectId
                object_id = ObjectId(user_id)
                logger.info(f"🔄 转换用户ID: {user_id} -> {object_id}")
                return PyObjectId(object_id)
        except Exception as e:
            logger.error(f"❌ 用户ID转换失败: {user_id} -> {e}")
            # 如果转换失败，生成一个新的ObjectId
            new_object_id = ObjectId()
            logger.warning(f"⚠️ 生成新的用户ID: {new_object_id}")
            return PyObjectId(new_object_id)

    def _get_user_risk_preference(self, user_id: str) -> str:
        """获取用户的风险偏好设置

        Args:
            user_id: 用户ID

        Returns:
            风险偏好: 'conservative'(保守) / 'neutral'(中性) / 'aggressive'(激进)
            默认返回 'neutral'
        """
        try:
            db = get_mongo_db()
            users_collection = db["users"]

            # 查询用户
            user = users_collection.find_one({"_id": ObjectId(user_id)})
            if user and "preferences" in user:
                risk_pref = user["preferences"].get("risk_preference", "neutral")
                logger.info(f"📊 获取用户 {user_id} 的风险偏好: {risk_pref}")
                return risk_pref

            logger.info(f"📊 用户 {user_id} 未设置风险偏好，使用默认值: neutral")
            return "neutral"
        except Exception as e:
            logger.warning(f"⚠️ 获取用户风险偏好失败: {e}，使用默认值: neutral")
            return "neutral"
    
    def _get_trading_graph(self, config: Dict[str, Any]):
        """获取或创建TradingAgents图实例（带缓存）— 已停用

        v3.0 起旧版 TradingAgentsGraph 引擎已停用。
        详见 docs/99-archive/deprecated-features/legacy-agents-deprecation.md

        Raises:
            RuntimeError: 始终抛出（方法已停用）
        """
        raise RuntimeError(
            "[已停用] AnalysisService._get_trading_graph 已停用，"
            "TradingAgentsGraph 不再实例化。"
            "详见 docs/99-archive/deprecated-features/legacy-agents-deprecation.md"
        )

    def _execute_analysis_sync_with_progress(self, task: AnalysisTask, progress_tracker: RedisProgressTracker) -> AnalysisResult:
        """同步执行分析任务（在线程池中运行，带进度跟踪）— 已停用

        v3.0 起旧版 legacy 引擎已停用。
        详见 docs/99-archive/deprecated-features/legacy-agents-deprecation.md

        Raises:
            RuntimeError: 始终抛出（方法已停用）
        """
        raise RuntimeError(
            "[已停用] AnalysisService._execute_analysis_sync_with_progress 已停用，"
            "task_id=%s 应改用 v2 引擎。"
            "详见 docs/99-archive/deprecated-features/legacy-agents-deprecation.md"
            % getattr(task, "task_id", "<unknown>")
        )

    def _execute_analysis_sync(self, task: AnalysisTask) -> AnalysisResult:
        """同步执行分析任务（在线程池中运行）— 已停用

        v3.0 起旧版 legacy 引擎已停用。
        详见 docs/99-archive/deprecated-features/legacy-agents-deprecation.md

        Raises:
            RuntimeError: 始终抛出（方法已停用）
        """
        raise RuntimeError(
            "[已停用] AnalysisService._execute_analysis_sync 已停用，"
            "task_id=%s 应改用 v2 引擎。"
            "详见 docs/99-archive/deprecated-features/legacy-agents-deprecation.md"
            % getattr(task, "task_id", "<unknown>")
        )

    async def _execute_single_analysis_async(self, task: AnalysisTask):
        """异步执行单股分析任务（在后台运行，不阻塞主线程）"""
        progress_tracker = None
        try:
            logger.info(f"🔄 开始执行分析任务: {task.task_id} - {task.symbol}")

            # 创建进度跟踪器
            progress_tracker = RedisProgressTracker(
                task_id=task.task_id,
                analysts=task.parameters.selected_analysts or ["market", "fundamentals"],
                research_depth=task.parameters.research_depth or "标准",
                llm_provider="dashscope"
            )

            # 缓存进度跟踪器
            self._progress_trackers[task.task_id] = progress_tracker

            # 初始化进度
            progress_tracker.update_progress("🚀 开始股票分析")
            await self._update_task_status_with_tracker(task.task_id, AnalysisStatus.PROCESSING, progress_tracker)

            # 在线程池中执行分析，避免阻塞事件循环
            import asyncio
            import concurrent.futures

            loop = asyncio.get_event_loop()

            # 使用线程池执行器运行同步的分析代码
            with concurrent.futures.ThreadPoolExecutor() as executor:
                result = await loop.run_in_executor(
                    executor,
                    self._execute_analysis_sync_with_progress,
                    task,
                    progress_tracker
                )

            # 标记完成
            progress_tracker.mark_completed("✅ 研究完成")
            await self._update_task_status_with_tracker(task.task_id, AnalysisStatus.COMPLETED, progress_tracker, result)

            # 记录 token 使用
            try:
                # 获取使用的模型信息
                quick_model = getattr(task.parameters, 'quick_analysis_model', None)
                deep_model = getattr(task.parameters, 'deep_analysis_model', None)

                # 优先使用深度分析模型，如果没有则使用快速分析模型
                model_name = deep_model or quick_model or "qwen-plus"

                # 根据模型名称确定供应商
                from app.services.llm_model_resolution import get_provider_by_model_name
                provider = get_provider_by_model_name(model_name)

                # 记录使用情况
                await self._record_token_usage(task, result, provider, model_name)
            except Exception as e:
                logger.error(f"⚠️  记录 token 使用失败: {e}")

            logger.info(f"✅ 分析任务完成: {task.task_id}")

        except Exception as e:
            logger.error(f"❌ 分析任务失败: {task.task_id} - {e}")

            # 标记失败
            if progress_tracker:
                progress_tracker.mark_failed(str(e))
                await self._update_task_status_with_tracker(task.task_id, AnalysisStatus.FAILED, progress_tracker)
            else:
                await self._update_task_status(task.task_id, AnalysisStatus.FAILED, 0, str(e))
        finally:
            # 清理进度跟踪器缓存
            if task.task_id in self._progress_trackers:
                del self._progress_trackers[task.task_id]

    async def submit_single_analysis(
        self,
        user_id: str,
        request: SingleAnalysisRequest
    ) -> Dict[str, Any]:
        """提交单股分析任务"""
        try:
            logger.info(f"📝 开始提交单股分析任务")
            logger.info(f"👤 用户ID: {user_id} (类型: {type(user_id)})")

            # 获取股票代码 (兼容旧字段)
            stock_symbol = request.get_symbol()
            logger.info(f"📊 股票代码: {stock_symbol}")
            logger.info(f"⚙️ 分析参数: {request.parameters}")

            # 生成任务ID
            task_id = str(uuid.uuid4())
            logger.info(f"🆔 生成任务ID: {task_id}")

            # 转换用户ID
            converted_user_id = self._convert_user_id(user_id)
            logger.info(f"🔄 转换后的用户ID: {converted_user_id} (类型: {type(converted_user_id)})")

            # 创建分析任务
            logger.info(f"🏗️ 开始创建AnalysisTask对象...")

            # 读取合并后的系统设置（ENV 优先 → DB），用于填充模型与并发/超时配置
            try:
                effective_settings = await config_provider.get_effective_system_settings()
            except Exception:
                effective_settings = {}

            # 填充分析参数中的模型（若请求未显式提供）
            params = request.parameters or AnalysisParameters()
            if not getattr(params, 'quick_analysis_model', None):
                params.quick_analysis_model = effective_settings.get("quick_analysis_model", "qwen-turbo")
            if not getattr(params, 'deep_analysis_model', None):
                params.deep_analysis_model = effective_settings.get("deep_analysis_model", "qwen-max")

            # 应用系统级并发与可见性超时（若提供）
            try:
                self.queue_service.user_concurrent_limit = int(effective_settings.get("max_concurrent_tasks", DEFAULT_USER_CONCURRENT_LIMIT))
                self.queue_service.global_concurrent_limit = int(effective_settings.get("max_concurrent_tasks", GLOBAL_CONCURRENT_LIMIT))
                self.queue_service.visibility_timeout = int(effective_settings.get("default_analysis_timeout", VISIBILITY_TIMEOUT_SECONDS))
            except Exception:
                # 使用默认值即可
                pass

            task = AnalysisTask(
                task_id=task_id,
                user_id=converted_user_id,
                symbol=stock_symbol,
                stock_code=stock_symbol,  # 兼容字段
                parameters=params,
                status=AnalysisStatus.PENDING
            )
            logger.info(f"✅ AnalysisTask对象创建成功")

            # 保存任务到数据库
            logger.info(f"💾 开始保存任务到数据库...")
            db = get_mongo_db()
            task_dict = task.model_dump(by_alias=True)
            logger.info(f"📄 任务字典: {task_dict}")
            await db.analysis_tasks.insert_one(task_dict)
            logger.info(f"✅ 任务已保存到数据库")

            # 🔥 单股分析：提交到队列，由 Worker 进程处理（与批量分析保持一致）
            logger.info(f"🚀 开始将任务提交到队列...")

            # 准备队列参数（直接传递分析参数，不嵌套）
            queue_params = task.parameters.dict() if task.parameters else {}

            # 添加任务元数据
            queue_params.update({
                "task_id": task.task_id,
                "symbol": task.symbol,
                "stock_code": task.symbol,  # 兼容字段
                "user_id": str(task.user_id),
                "created_at": task.created_at.isoformat() if task.created_at else None
            })

            # 调用队列服务
            await self.queue_service.enqueue_task(
                user_id=str(converted_user_id),
                symbol=stock_symbol,
                params=queue_params,
                batch_id=None,  # 单股分析没有批次ID
                task_id=task_id  # 使用已生成的任务ID
            )

            logger.info(f"✅ 任务已提交到队列，任务ID: {task_id}")
            logger.info(f"🎉 单股分析任务提交完成: {task_id} - {stock_symbol}")

            return {
                "task_id": task_id,
                "symbol": stock_symbol,
                "stock_code": stock_symbol,  # 兼容字段
                "status": AnalysisStatus.PENDING,
                "message": "任务已提交到队列，等待 Worker 处理"
            }
            
        except Exception as e:
            logger.error(f"提交单股分析任务失败: {e}")
            raise
    
    async def submit_batch_analysis(
        self, 
        user_id: str, 
        request: BatchAnalysisRequest
    ) -> Dict[str, Any]:
        """提交批量分析任务"""
        try:
            # 生成批次ID
            batch_id = str(uuid.uuid4())
            
            # 转换用户ID
            converted_user_id = self._convert_user_id(user_id)

            # 读取系统设置，填充模型参数并应用并发/超时配置
            try:
                effective_settings = await config_provider.get_effective_system_settings()
            except Exception:
                effective_settings = {}

            params = request.parameters or AnalysisParameters()
            if not getattr(params, 'quick_analysis_model', None):
                params.quick_analysis_model = effective_settings.get("quick_analysis_model", "qwen-turbo")
            if not getattr(params, 'deep_analysis_model', None):
                params.deep_analysis_model = effective_settings.get("deep_analysis_model", "qwen-max")

            try:
                self.queue_service.user_concurrent_limit = int(effective_settings.get("max_concurrent_tasks", DEFAULT_USER_CONCURRENT_LIMIT))
                self.queue_service.global_concurrent_limit = int(effective_settings.get("max_concurrent_tasks", GLOBAL_CONCURRENT_LIMIT))
                self.queue_service.visibility_timeout = int(effective_settings.get("default_analysis_timeout", VISIBILITY_TIMEOUT_SECONDS))
            except Exception:
                pass

            # 创建批次记录
            # 获取股票代码列表 (兼容旧字段)
            stock_symbols = request.get_symbols()

            batch = AnalysisBatch(
                batch_id=batch_id,
                user_id=converted_user_id,
                title=request.title,
                description=request.description,
                total_tasks=len(stock_symbols),
                parameters=params,
                status=BatchStatus.PENDING
            )

            # 创建任务列表
            tasks = []
            for symbol in stock_symbols:
                task_id = str(uuid.uuid4())
                task = AnalysisTask(
                    task_id=task_id,
                    batch_id=batch_id,
                    user_id=converted_user_id,
                    symbol=symbol,
                    stock_code=symbol,  # 兼容字段
                    parameters=batch.parameters,
                    status=AnalysisStatus.PENDING
                )
                tasks.append(task)
            
            # 保存到数据库
            db = get_mongo_db()
            await db.analysis_batches.insert_one(batch.dict(by_alias=True))
            await db.analysis_tasks.insert_many([task.dict(by_alias=True) for task in tasks])
            
            # 提交任务到队列
            for task in tasks:
                # 准备队列参数（直接传递分析参数，不嵌套）
                queue_params = task.parameters.dict() if task.parameters else {}

                # 添加任务元数据
                queue_params.update({
                    "task_id": task.task_id,
                    "symbol": task.symbol,
                    "stock_code": task.symbol,  # 兼容字段
                    "user_id": str(task.user_id),
                    "batch_id": task.batch_id,
                    "created_at": task.created_at.isoformat() if task.created_at else None
                })

                # 调用队列服务
                await self.queue_service.enqueue_task(
                    user_id=str(converted_user_id),
                    symbol=task.symbol,
                    params=queue_params,
                    batch_id=task.batch_id
                )
            
            logger.info(f"批量分析任务已提交: {batch_id} - {len(tasks)}个股票")
            
            return {
                "batch_id": batch_id,
                "total_tasks": len(tasks),
                "status": BatchStatus.PENDING,
                "message": f"已提交{len(tasks)}个分析任务到队列"
            }
            
        except Exception as e:
            logger.error(f"提交批量分析任务失败: {e}")
            raise
    
    async def execute_analysis_task(
        self,
        task: AnalysisTask,
        progress_callback: Optional[Callable[[int, str], None]] = None
    ) -> AnalysisResult:
        """执行单个分析任务 — 已停用

        v3.0 起旧版 legacy 引擎已停用。
        详见 docs/99-archive/deprecated-features/legacy-agents-deprecation.md

        Raises:
            RuntimeError: 始终抛出（方法已停用）
        """
        raise RuntimeError(
            "[已停用] AnalysisService.execute_analysis_task 已停用，"
            "task_id=%s 请改用 UnifiedAnalysisEngine.execute_task。"
            "详见 docs/99-archive/deprecated-features/legacy-agents-deprecation.md"
            % getattr(task, "task_id", "<unknown>")
        )
    
    async def _update_task_status(
        self,
        task_id: str,
        status: AnalysisStatus,
        progress: int,
        result: Optional[AnalysisResult] = None,
    ) -> None:
        """更新任务状态（委托至拆分的工具函数）"""
        try:
            from app.services.analysis.status_update_utils import perform_update_task_status
            await perform_update_task_status(task_id, status, progress, result)
        except Exception as e:
            logger.error(f"更新任务状态失败: {task_id} - {e}")

    async def _update_task_status_with_tracker(
        self,
        task_id: str,
        status: AnalysisStatus,
        progress_tracker: RedisProgressTracker,
        result: Optional[AnalysisResult] = None,
    ) -> None:
        """使用进度跟踪器更新任务状态（委托至拆分的工具函数）"""
        try:
            from app.services.analysis.status_update_utils import perform_update_task_status_with_tracker
            await perform_update_task_status_with_tracker(task_id, status, progress_tracker, result)
        except Exception as e:
            logger.error(f"更新任务状态失败: {task_id} - {e}")

    async def get_task_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """获取任务状态"""
        try:
            # 先检查内存中的进度跟踪器
            if task_id in self._progress_trackers:
                progress_tracker = self._progress_trackers[task_id]
                progress_data = progress_tracker.to_dict()

                # 从数据库获取任务基本信息
                db = get_mongo_db()
                task = await db.analysis_tasks.find_one({"task_id": task_id})

                if task:
                    # 合并数据库信息和进度跟踪器信息
                    return {
                        "task_id": task_id,
                        "user_id": task.get("user_id"),
                        "symbol": task.get("stock_symbol") or task.get("symbol"),
                        "stock_code": task.get("stock_symbol") or task.get("symbol"),  # 兼容字段
                        "status": progress_data["status"],
                        "progress": progress_data["progress"],
                        "current_step": progress_data["current_step"],
                        "message": progress_data["message"],
                        "elapsed_time": progress_data["elapsed_time"],
                        "remaining_time": progress_data["remaining_time"],
                        "estimated_total_time": progress_data.get("estimated_total_time", 0),
                        "steps": progress_data["steps"],
                        "start_time": progress_data["start_time"],
                        "end_time": None,
                        "last_update": progress_data["last_update"],
                        "parameters": task.get("parameters", {}),
                        "execution_time": None,
                        "tokens_used": None,
                        "result_data": task.get("result"),
                        "error_message": None
                    }

            # 从Redis缓存获取
            redis_service = get_redis_service()
            progress_key = RedisKeys.TASK_PROGRESS.format(task_id=task_id)
            cached_status = await redis_service.get_json(progress_key)

            if cached_status:
                return cached_status

            # 从数据库获取
            db = get_mongo_db()
            task = await db.analysis_tasks.find_one({"task_id": task_id})

            if task:
                # 计算已用时间
                elapsed_time = 0
                remaining_time = 0
                estimated_total_time = 0

                if task.get("started_at"):
                    from datetime import datetime
                    start_time = task.get("started_at")
                    if task.get("completed_at"):
                        # 任务已完成
                        elapsed_time = (task.get("completed_at") - start_time).total_seconds()
                        estimated_total_time = elapsed_time  # 已完成任务的总时长就是已用时间
                        remaining_time = 0
                    else:
                        # 任务进行中
                        elapsed_time = (datetime.utcnow() - start_time).total_seconds()

                        # 使用任务的预估时长，如果没有则使用默认值（5分钟）
                        estimated_total_time = task.get("estimated_duration", 300)

                        # 预计剩余 = 预估总时长 - 已用时间
                        remaining_time = max(0, estimated_total_time - elapsed_time)

                return {
                    "task_id": task_id,
                    "status": task.get("status"),
                    "progress": task.get("progress", 0),
                    "current_step": task.get("current_step", ""),
                    "message": task.get("message", ""),
                    "elapsed_time": elapsed_time,
                    "remaining_time": remaining_time,
                    "estimated_total_time": estimated_total_time,
                    "start_time": task.get("started_at").isoformat() if task.get("started_at") else None,
                    "updated_at": task.get("updated_at", "").isoformat() if task.get("updated_at") else None,
                    "result_data": task.get("result")
                }

            return None

        except Exception as e:
            logger.error(f"获取任务状态失败: {task_id} - {e}")
            return None
    
    async def cancel_task(self, task_id: str) -> bool:
        """取消任务"""
        try:
            # 更新任务状态
            await self._update_task_status(task_id, AnalysisStatus.CANCELLED, 0)
            
            # 从队列中移除（如果还在队列中）
            await self.queue_service.remove_task(task_id)
            
            logger.info(f"任务已取消: {task_id}")
            return True
            
        except Exception as e:
            logger.error(f"取消任务失败: {task_id} - {e}")
            return False

    async def _record_token_usage(
        self,
        task: AnalysisTask,
        result: AnalysisResult,
        provider: str,
        model_name: str
    ):
        """记录 token 使用情况"""
        try:
            # 从结果中提取 token 使用信息
            # 注意：这里需要从 LLM 响应中获取实际的 token 使用量
            # 目前使用估算值
            input_tokens = result.tokens_used // 2 if result.tokens_used > 0 else 0
            output_tokens = result.tokens_used - input_tokens if result.tokens_used > 0 else 0

            # 如果没有 token 使用信息，使用默认估算
            if result.tokens_used == 0:
                # 根据分析类型估算
                input_tokens = 2000  # 默认输入 token
                output_tokens = 1000  # 默认输出 token

            # 获取模型价格配置
            from app.services.config_service import config_service
            config = await config_service.get_system_config()

            # 查找对应的 LLM 配置
            llm_config = None
            if config and config.llm_configs:
                for cfg in config.llm_configs:
                    if cfg.provider == provider and cfg.model_name == model_name:
                        llm_config = cfg
                        break

            # 计算成本
            cost = 0.0
            currency = "CNY"  # 默认货币单位
            if llm_config:
                input_price = llm_config.input_price_per_1k or 0.0
                output_price = llm_config.output_price_per_1k or 0.0
                cost = (input_tokens / 1000 * input_price) + (output_tokens / 1000 * output_price)
                currency = llm_config.currency or "CNY"

            # 创建使用记录
            usage_record = UsageRecord(
                timestamp=datetime.now().isoformat(),
                provider=provider,
                model_name=model_name,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost=cost,
                currency=currency,
                session_id=task.task_id,
                analysis_type="stock_analysis",
                stock_code=task.symbol
            )

            # 保存到数据库
            success = await self.usage_service.add_usage_record(usage_record)

            if success:
                logger.info(f"💰 记录使用成本: {provider}/{model_name} - ¥{cost:.4f}")
            else:
                logger.warning(f"⚠️  记录使用成本失败")

        except Exception as e:
            logger.error(f"❌ 记录 token 使用失败: {e}")


# 全局分析服务实例（延迟初始化）
analysis_service: Optional[AnalysisService] = None


def get_analysis_service() -> AnalysisService:
    """获取分析服务实例（延迟初始化）"""
    global analysis_service
    if analysis_service is None:
        analysis_service = AnalysisService()
    return analysis_service
