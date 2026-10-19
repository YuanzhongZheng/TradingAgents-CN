"""PortfolioService.PositionMixin。"""
from app.services.portfolio.support import (
    asyncio,
    datetime,
    Dict,
    Any,
    Optional,
    List,
    ObjectId,
    PositionSource,
    PositionCreate,
    PositionUpdate,
    PositionResponse,
    PositionChangeType,
    PositionOperationType,
    PositionOperationRequest,
    now_tz,
    logger,
)

class PositionMixin:

    async def _clear_existing_real_positions_for_sync(self, user_id: str) -> Dict[str, Any]:
        """清空当前用户的实盘持仓，并把持仓成本退回到现金中，用于 QMT 覆盖同步。"""
        positions = await self.db[self.positions_collection].find(
            {"user_id": user_id, "quantity": {"$gt": 0}}
        ).to_list(None)

        if not positions:
            return {
                "deleted_positions": 0,
                "deleted_changes": 0,
                "restored_cash": {"CNY": 0.0, "HKD": 0.0, "USD": 0.0},
            }

        restored_cash: Dict[str, float] = {"CNY": 0.0, "HKD": 0.0, "USD": 0.0}
        for position in positions:
            currency = position.get("currency", "CNY")
            position_cost = float(position.get("quantity", 0) or 0) * float(position.get("cost_price", 0.0) or 0.0)
            restored_cash[currency] = round(restored_cash.get(currency, 0.0) + position_cost, 2)

        deleted_changes = await self.db[self.position_changes_collection].delete_many({"user_id": user_id})
        deleted_positions = await self.db[self.positions_collection].delete_many({"user_id": user_id})

        account = await self.get_or_create_account(user_id)
        current_cash = account.get("cash", {})
        update_doc: Dict[str, Any] = {"updated_at": now_tz()}
        cash_updated = False
        for currency, amount in restored_cash.items():
            if amount <= 0:
                continue
            update_doc[f"cash.{currency}"] = round(current_cash.get(currency, 0.0) + amount, 2)
            cash_updated = True

        if cash_updated:
            await self.db[self.accounts_collection].update_one(
                {"user_id": user_id},
                {"$set": update_doc}
            )

        logger.info(
            "QMT 覆盖同步前清空本地实盘持仓: user=%s, 删除 %s 个持仓, %s 条变动, 退回资金 %s",
            user_id,
            deleted_positions.deleted_count,
            deleted_changes.deleted_count,
            restored_cash,
        )
        return {
            "deleted_positions": deleted_positions.deleted_count,
            "deleted_changes": deleted_changes.deleted_count,
            "restored_cash": restored_cash,
        }

    def _build_qmt_import_positions(self, positions_df: Any) -> List[PositionCreate]:
        """将 QMT 持仓 DataFrame 转为系统导入使用的 PositionCreate 列表。"""
        positions: List[PositionCreate] = []
        if positions_df is None:
            return positions

        for _, row in positions_df.iterrows():
            code = str(row.get("code", "")).strip().zfill(6)
            if not code or len(code) != 6:
                continue

            try:
                quantity = int(float(row.get("quantity", 0)))
                cost_price = float(row.get("cost_price", 0))
            except (TypeError, ValueError):
                continue

            if quantity <= 0 or cost_price <= 0:
                continue

            buy_date = row.get("buy_date")
            notes = row.get("notes")
            positions.append(PositionCreate(
                code=code,
                name=(str(row.get("name", "")).strip() or None),
                market=str(row.get("market", "CN") or "CN").strip() or "CN",
                quantity=quantity,
                cost_price=round(cost_price, 4),
                buy_date=buy_date if isinstance(buy_date, datetime) else None,
                notes=str(notes).strip() if notes not in (None, "") else None,
            ))

        return positions

    async def _sync_qmt_cash_to_account(self, user_id: str, asset_summary: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """将 QMT 账户可用现金镜像到本地资金账户。"""
        asset_summary = asset_summary or {}
        qmt_cash = asset_summary.get("cash")
        if qmt_cash is None:
            return {
                "cash_synced": False,
                "synced_currency": None,
                "synced_cash": None,
            }

        synced_cash = round(float(qmt_cash), 2)
        await self.get_or_create_account(user_id)
        await self.db[self.accounts_collection].update_one(
            {"user_id": user_id},
            {"$set": {"cash.CNY": synced_cash, "updated_at": now_tz()}}
        )

        logger.info("QMT 现金同步完成: user=%s, CNY=%s", user_id, synced_cash)
        return {
            "cash_synced": True,
            "synced_currency": "CNY",
            "synced_cash": synced_cash,
        }

    async def preview_qmt_position_import(
        self,
        user_id: str,
        account_id: str,
        account_type: str = "STOCK",
    ) -> Dict[str, Any]:
        """预览 QMT 持仓覆盖同步对当前账户的影响。"""
        from app.services.data_sources.qmt_adapter import QMTAdapter

        adapter = QMTAdapter()
        snapshot = await asyncio.to_thread(adapter.query_account_snapshot, account_id, account_type)
        if snapshot is None:
            raise ValueError("QMT 未查询到账户信息，请确认 QMT 已登录且账号可用")

        positions_df = snapshot.get("positions_df")
        asset_summary = snapshot.get("asset") or {}
        importable_positions = self._build_qmt_import_positions(positions_df)

        current_position_count = await self.db[self.positions_collection].count_documents(
            {"user_id": user_id, "quantity": {"$gt": 0}}
        )
        current_change_count = await self.db[self.position_changes_collection].count_documents({"user_id": user_id})
        account_summary = await self.get_account_summary(user_id)

        qmt_position_count = 0 if positions_df is None else len(positions_df)
        qmt_cash = asset_summary.get("cash")
        qmt_market_value = asset_summary.get("market_value")
        qmt_total_asset = asset_summary.get("total_asset")

        return {
            "account_id": account_id,
            "account_type": account_type,
            "will_replace_position_count": current_position_count,
            "will_clear_change_count": current_change_count,
            "local_cash": round(account_summary.cash.get("CNY", 0.0), 2),
            "local_total_assets": round(account_summary.total_assets.get("CNY", 0.0), 2),
            "qmt_position_count": qmt_position_count,
            "qmt_importable_count": len(importable_positions),
            "qmt_cash": round(float(qmt_cash), 2) if qmt_cash is not None else None,
            "qmt_market_value": round(float(qmt_market_value), 2) if qmt_market_value is not None else None,
            "qmt_total_asset": round(float(qmt_total_asset), 2) if qmt_total_asset is not None else None,
            "will_sync_cash": qmt_cash is not None,
            "cash_currency": "CNY",
            "sync_note": "将覆盖本地实盘持仓，并把 CNY 可用现金更新为 QMT 账户现金；初始资金和累计入出金记录保持不变。",
        }

    # ==================== 持仓管理 ====================

    async def get_positions(
        self, 
        user_id: str, 
        source: str = "all",
        include_market_data: bool = True
    ) -> List[PositionResponse]:
        """
        获取用户持仓列表
        
        Args:
            user_id: 用户ID
            source: 数据来源 (all/real/paper)
            include_market_data: 是否包含实时行情
        """
        positions = []

        # 获取用户持仓（只获取 quantity > 0 的持仓）
        if source in ["all", "real"]:
            real_positions = await self.db[self.positions_collection].find(
                {"user_id": user_id, "quantity": {"$gt": 0}}
            ).to_list(None)

            for p in real_positions:
                pos = await self._enrich_position(p, "real", include_market_data)
                positions.append(pos)

        # 获取模拟交易持仓（只获取 quantity > 0 的持仓）
        if source in ["all", "paper"]:
            paper_positions = await self.db[self.paper_positions_collection].find(
                {"user_id": user_id, "quantity": {"$gt": 0}}
            ).to_list(None)
            
            for p in paper_positions:
                pos = await self._enrich_position(p, "paper", include_market_data)
                positions.append(pos)

        return positions

    async def get_history_positions(
        self,
        user_id: str,
        source: str = "real",
        limit: int = 50,
        skip: int = 0
    ) -> Dict[str, Any]:
        """
        获取历史持仓（已清仓的记录）
        按股票代码聚合，显示每只股票的交易汇总
        只显示已完全清仓的持仓（买入数量 = 卖出数量）
        """
        # 从变动记录中聚合历史持仓数据
        pipeline = [
            {"$match": {"user_id": user_id}},
            {"$group": {
                "_id": {"code": "$code", "market": "$market"},
                "name": {"$last": "$name"},
                "currency": {"$last": "$currency"},
                "first_buy_date": {"$min": {"$cond": [
                    {"$in": ["$change_type", ["buy", "add"]]},
                    "$trade_time", None
                ]}},
                "last_sell_date": {"$max": {"$cond": [
                    {"$in": ["$change_type", ["sell", "reduce"]]},
                    "$trade_time", None
                ]}},
                "total_buy_qty": {"$sum": {"$cond": [
                    {"$in": ["$change_type", ["buy", "add"]]},
                    {"$abs": "$quantity_change"}, 0
                ]}},
                "total_sell_qty": {"$sum": {"$cond": [
                    {"$in": ["$change_type", ["sell", "reduce"]]},
                    {"$abs": "$quantity_change"}, 0
                ]}},
                "total_buy_amount": {"$sum": {"$cond": [
                    {"$in": ["$change_type", ["buy", "add"]]},
                    {"$abs": "$cash_change"}, 0
                ]}},
                "total_sell_amount": {"$sum": {"$cond": [
                    {"$in": ["$change_type", ["sell", "reduce"]]},
                    {"$abs": "$cash_change"}, 0
                ]}},
                "total_realized_pnl": {"$sum": {"$ifNull": ["$realized_profit", 0]}},
                "avg_buy_price": {"$avg": {"$cond": [
                    {"$in": ["$change_type", ["buy", "add"]]},
                    "$cost_price_after", None
                ]}},
                "avg_sell_price": {"$avg": {"$cond": [
                    {"$in": ["$change_type", ["sell", "reduce"]]},
                    "$trade_price", None
                ]}}
            }},
            # 只显示已完全清仓的（买入数量 = 卖出数量）
            {"$match": {
                "$expr": {
                    "$and": [
                        {"$gt": ["$total_sell_qty", 0]},
                        {"$eq": ["$total_buy_qty", "$total_sell_qty"]}
                    ]
                }
            }},
            {"$sort": {"last_sell_date": -1}},
            {"$skip": skip},
            {"$limit": limit}
        ]

        results = await self.db[self.position_changes_collection].aggregate(pipeline).to_list(None)

        # 计算总数
        count_pipeline = [
            {"$match": {"user_id": user_id}},
            {"$group": {
                "_id": {"code": "$code", "market": "$market"},
                "total_buy_qty": {"$sum": {"$cond": [
                    {"$in": ["$change_type", ["buy", "add"]]},
                    {"$abs": "$quantity_change"}, 0
                ]}},
                "total_sell_qty": {"$sum": {"$cond": [
                    {"$in": ["$change_type", ["sell", "reduce"]]},
                    {"$abs": "$quantity_change"}, 0
                ]}}
            }},
            {"$match": {
                "$expr": {
                    "$and": [
                        {"$gt": ["$total_sell_qty", 0]},
                        {"$eq": ["$total_buy_qty", "$total_sell_qty"]}
                    ]
                }
            }},
            {"$count": "total"}
        ]
        count_result = await self.db[self.position_changes_collection].aggregate(count_pipeline).to_list(None)
        total = count_result[0]["total"] if count_result else 0

        items = []
        for r in results:
            # 计算持有天数
            first_buy = r.get("first_buy_date")
            last_sell = r.get("last_sell_date")
            hold_days = 0
            if first_buy and last_sell:
                # 🔥 处理字符串类型的日期（MongoDB可能返回字符串）
                if isinstance(first_buy, str):
                    first_buy = datetime.fromisoformat(first_buy.replace('Z', '+00:00'))
                if isinstance(last_sell, str):
                    last_sell = datetime.fromisoformat(last_sell.replace('Z', '+00:00'))
                delta = last_sell - first_buy
                hold_days = delta.days

            # 计算盈亏百分比
            total_buy_amount = r.get("total_buy_amount", 0)
            total_realized_pnl = r.get("total_realized_pnl", 0)
            realized_pnl_pct = 0
            if total_buy_amount > 0:
                realized_pnl_pct = (total_realized_pnl / total_buy_amount) * 100

            items.append({
                "id": f"{r['_id']['code']}_{r['_id']['market']}",
                "code": r["_id"]["code"],
                "name": r.get("name"),
                "market": r["_id"]["market"],
                "currency": r.get("currency", "CNY"),
                "total_buy_qty": r.get("total_buy_qty", 0),
                "total_sell_qty": r.get("total_sell_qty", 0),
                "avg_buy_price": round(r.get("avg_buy_price") or 0, 2),
                "avg_sell_price": round(r.get("avg_sell_price") or 0, 2),
                "first_buy_date": first_buy.isoformat() if first_buy else None,
                "last_trade_date": last_sell.isoformat() if last_sell else None,
                "realized_pnl": round(total_realized_pnl, 2),
                "realized_pnl_pct": round(realized_pnl_pct, 2),
                "hold_days": hold_days,
                "cleared_at": last_sell.isoformat() if last_sell else None
            })

        return {
            "items": items,
            "total": total,
            "limit": limit,
            "skip": skip
        }

    async def _enrich_position(
        self, 
        position: Dict, 
        source: str,
        include_market_data: bool = True
    ) -> PositionResponse:
        """丰富持仓数据（添加实时行情）"""
        code = position.get("code", "")
        market = position.get("market", "CN")
        quantity = int(position.get("quantity", 0))
        
        # 获取成本价
        if source == "paper":
            cost_price = float(position.get("avg_cost", 0.0))
            original_avg_cost = cost_price  # 模拟盘无原始成本区分
        else:
            cost_price = float(position.get("cost_price", 0.0))
            # 向后兼容：无 original_avg_cost 时使用 cost_price
            original_avg_cost = position.get("original_avg_cost")
            if original_avg_cost is None:
                original_avg_cost = cost_price
            else:
                original_avg_cost = float(original_avg_cost)

        # 获取实时价格
        current_price = None
        market_value = None
        unrealized_pnl = None
        unrealized_pnl_pct = None

        if include_market_data:
            current_price = await self._get_stock_price(code, market)
            if current_price and current_price > 0:
                market_value = round(current_price * quantity, 2)
                cost_value = cost_price * quantity
                if cost_value > 0:
                    unrealized_pnl = round(market_value - cost_value, 2)
                    unrealized_pnl_pct = round((unrealized_pnl / cost_value) * 100, 2)

        # 获取股票名称
        name = position.get("name")
        if not name:
            name = await self._get_stock_name(code, market)

        # 🔥 获取行业信息：如果为空，尝试从数据库获取并更新
        industry = position.get("industry")
        if not industry or industry == "未知":
            logger.info(f"🔄 [丰富持仓] 持仓 {code} ({source}) 行业为空或'未知'，尝试获取...")
            industry = await self._get_stock_industry(code, market)
            logger.info(f"📊 [丰富持仓] 持仓 {code} 获取到的行业: {industry}")
            
            # 如果获取到了行业信息，更新到数据库
            if industry:
                try:
                    position_id = position.get("_id")
                    if position_id:
                        # 根据数据源选择对应的集合
                        collection_name = self.paper_positions_collection if source == "paper" else self.positions_collection
                        
                        update_result = await self.db[collection_name].update_one(
                            {"_id": position_id},
                            {"$set": {"industry": industry, "updated_at": now_tz()}}
                        )
                        if update_result.modified_count > 0:
                            logger.info(f"✅ [丰富持仓] 已更新持仓 {code} ({source}) 的行业信息: {industry}")
                        else:
                            logger.debug(f"ℹ️ [丰富持仓] 持仓 {code} 行业信息未修改（可能已存在）")
                except Exception as e:
                    logger.warning(f"⚠️ [丰富持仓] 更新持仓 {code} 行业信息失败: {e}")
            else:
                # 如果无法获取，使用"未知"
                logger.warning(f"⚠️ [丰富持仓] 持仓 {code} 无法获取行业信息，使用'未知'")
                industry = "未知"

        return PositionResponse(
            id=str(position.get("_id", "")),
            code=code,
            name=name,
            market=market,
            currency=position.get("currency", "CNY"),
            quantity=quantity,
            cost_price=cost_price,
            original_avg_cost=round(original_avg_cost, 4) if original_avg_cost is not None else None,
            original_cost_price=position.get("original_cost_price"),
            price_adjust_type=position.get("price_adjust_type"),
            current_price=current_price,
            market_value=market_value,
            unrealized_pnl=unrealized_pnl,
            unrealized_pnl_pct=unrealized_pnl_pct,
            buy_date=position.get("buy_date"),
            industry=industry,
            notes=position.get("notes"),
            source=source,
            created_at=position.get("created_at", now_tz()),
            updated_at=position.get("updated_at", now_tz())
        )

    async def add_position(
        self,
        user_id: str,
        data: PositionCreate,
        source: PositionSource = PositionSource.MANUAL,
        validate_input: bool = True,
    ) -> PositionResponse:
        """添加用户持仓（如果已有持仓则自动加仓）"""
        # 手工录入时仍然保留历史数据校验；QMT 快照同步会跳过这层校验，直接按已知持仓写入。
        adjust_info = None  # 复权转换信息
        if validate_input:
            current_price = await self._get_stock_price(data.code, data.market)
            if not current_price or current_price <= 0:
                raise ValueError(
                    f"股票 {data.code} 暂无行情数据，无法添加持仓。请先在「股票筛选」或「单股分析」中同步该股票的历史数据后再添加。"
                )

            # 🔥 必须填写买入日期，并校验成本价是否在该日历史高低价范围内
            if not data.buy_date:
                raise ValueError("添加持仓时必须填写买入日期，以便校验成本价是否正确。")
            adjust_info = await self._validate_cost_price_against_historical(
                code=data.code,
                market=data.market,
                buy_date=data.buy_date,
                cost_price=data.cost_price
            )

        # ---- 复权价格转换 ----
        # adjust_info 不为 None 说明用户输入的是不复权价格，需要转为前复权保存
        original_cost_price = None  # 保存原始不复权价格
        price_adjust_type = None    # 价格标注
        effective_cost_price = data.cost_price  # 实际用于计算的成本价（前复权）

        if adjust_info and adjust_info.get("adjust_type") == "none":
            original_cost_price = data.cost_price  # 保存用户原始输入的不复权价格
            price_adjust_type = "none"
            qfq_price = adjust_info.get("qfq_price")
            if qfq_price is not None and qfq_price > 0:
                effective_cost_price = qfq_price  # 使用转换后的前复权价格
                logger.info(
                    f"🔄 成本价转换：不复权 {data.cost_price:.2f} → 前复权 {qfq_price:.2f}"
                    f"（复权因子={adjust_info.get('adj_factor', 'N/A')}）"
                )
            else:
                # 无法转换，保留不复权价格（后续盈亏计算可能有小误差）
                effective_cost_price = data.cost_price
                logger.warning(
                    f"⚠️ 无法将不复权价格 {data.cost_price:.2f} 转为前复权，"
                    f"将以不复权价格保存（盈亏计算可能存在分红导致的误差）"
                )

        # 获取股票名称和行业
        name = data.name
        logger.debug(f"📝 [添加持仓] 股票代码: {data.code}, 市场: {data.market}, 已有名称: {name}")
        
        # 🔥 修复：无论名称是否为空，都尝试获取行业信息
        # 如果名称为空，先获取名称；如果名称不为空，也要获取行业信息
        if not name:
            name = await self._get_stock_name(data.code, data.market)
            logger.debug(f"📝 [添加持仓] 获取到的名称: {name}")
        
        # 始终尝试获取行业信息（即使名称已存在）
        industry = await self._get_stock_industry(data.code, data.market)
        logger.info(f"📝 [添加持仓] 股票 {data.code} 行业信息: {industry}")

        # 计算持仓成本（使用转换后的前复权价格）
        currency = self._get_currency_by_market(data.market)
        position_cost = data.quantity * effective_cost_price

        # 🔥 计算买入手续费
        trade_amount = data.quantity * effective_cost_price
        market_rules = await self._get_real_market_rules(data.market)
        commission_info = self._calculate_real_commission(data.market, "buy", trade_amount, market_rules or {})
        total_commission = commission_info["commission"]
        total_cost = position_cost + total_commission  # 总支出 = 成交金额 + 手续费

        # 从资金账户中扣除成本+手续费
        account = await self.get_or_create_account(user_id)
        current_cash = account.get("cash", {}).get(currency, 0.0)

        if current_cash < total_cost:
            raise ValueError(f"可用资金不足。当前可用: {current_cash:.2f} {currency}，需要: {total_cost:.2f} {currency}（含手续费 {total_commission:.2f}）")

        # 检查是否已有该股票的持仓（同一用户、同一股票代码、同一市场）
        existing_position = await self.db[self.positions_collection].find_one({
            "user_id": user_id,
            "code": data.code,
            "market": data.market,
            "quantity": {"$gt": 0}  # 只查找数量大于0的持仓（未清仓的）
        })

        if existing_position:
            # 已有持仓，执行加仓操作
            old_quantity = existing_position.get("quantity", 0)
            old_cost_price = existing_position.get("cost_price", 0.0)
            old_total_cost = old_quantity * old_cost_price

            # 计算新的持仓数量和平均成本价
            new_quantity = old_quantity + data.quantity
            new_total_cost = old_total_cost + position_cost
            new_cost_price = new_total_cost / new_quantity if new_quantity > 0 else 0.0

            # 扣除资金（含手续费）
            new_cash = current_cash - total_cost
            await self.db[self.accounts_collection].update_one(
                {"user_id": user_id},
                {"$set": {f"cash.{currency}": new_cash, "updated_at": now_tz()}}
            )

            # 更新持仓记录（加仓时 cost_price 与 original_avg_cost 均为加权均价）
            now = now_tz()
            update_fields = {
                "quantity": new_quantity,
                "cost_price": round(new_cost_price, 4),
                "original_avg_cost": round(new_cost_price, 4),
                "updated_at": now,
                "notes": data.notes if data.notes else existing_position.get("notes")
            }
            # 加仓时如有不复权标注，也保存到持仓记录
            if original_cost_price is not None:
                update_fields["original_cost_price"] = original_cost_price
                update_fields["price_adjust_type"] = price_adjust_type
            await self.db[self.positions_collection].update_one(
                {"_id": existing_position["_id"]},
                {"$set": update_fields}
            )

            # 记录持仓变动（加仓，含手续费明细）
            change_description = f"加仓: +{data.quantity}股 @ {effective_cost_price:.2f}"
            if total_commission > 0:
                change_description += f"（手续费 ¥{total_commission:.2f}）"
            await self.record_position_change(
                user_id=user_id,
                change_type=PositionChangeType.BUY,
                code=data.code,
                name=name or existing_position.get("name") or data.code,
                market=data.market,
                currency=currency,
                position_id=str(existing_position["_id"]),
                quantity_before=old_quantity,
                cost_price_before=old_cost_price,
                quantity_after=new_quantity,
                cost_price_after=new_cost_price,
                description=change_description,
                extra_data={"commission": commission_info} if total_commission > 0 else None
            )

            logger.info(f"✅ 加仓成功: {user_id} - {data.code}，{old_quantity} → {new_quantity}股，成本价: {old_cost_price:.2f} → {new_cost_price:.2f}，扣除资金: {total_cost:.2f} {currency}（含手续费 {total_commission:.2f}）")

            # 返回更新后的持仓
            updated_position = await self.db[self.positions_collection].find_one({"_id": existing_position["_id"]})
            return await self._enrich_position(updated_position, "real")

        # 没有已有持仓，新建持仓记录
        # 扣除资金（含手续费）
        new_cash = current_cash - total_cost
        await self.db[self.accounts_collection].update_one(
            {"user_id": user_id},
            {"$set": {f"cash.{currency}": new_cash, "updated_at": now_tz()}}
        )

        now = now_tz()
        position_doc = {
            "user_id": user_id,
            "code": data.code,
            "name": name,
            "market": data.market,
            "currency": currency,
            "quantity": data.quantity,
            "cost_price": effective_cost_price,
            "original_avg_cost": effective_cost_price,
            "original_cost_price": original_cost_price,
            "price_adjust_type": price_adjust_type,
            "buy_date": data.buy_date,
            "industry": industry,
            "notes": data.notes,
            "source": source.value,
            "created_at": now,
            "updated_at": now
        }

        result = await self.db[self.positions_collection].insert_one(position_doc)
        position_doc["_id"] = result.inserted_id

        # 记录持仓变动（含手续费明细）
        change_description = f"新建持仓: {data.notes}" if data.notes else "新建持仓"
        if total_commission > 0:
            change_description += f"（手续费 ¥{total_commission:.2f}）"
        await self.record_position_change(
            user_id=user_id,
            change_type=PositionChangeType.BUY,
            code=data.code,
            name=name or data.code,
            market=data.market,
            currency=currency,
            position_id=str(result.inserted_id),
            quantity_before=0,
            cost_price_before=0.0,
            quantity_after=data.quantity,
            cost_price_after=effective_cost_price,
            description=change_description,
            trade_time=data.buy_date,
            extra_data={"commission": commission_info} if total_commission > 0 else None
        )

        logger.info(f"✅ 新建持仓成功: {user_id} - {data.code}，扣除资金: {total_cost:.2f} {currency}（含手续费 {total_commission:.2f}）")
        return await self._enrich_position(position_doc, "real")

    async def operate_position(
        self,
        user_id: str,
        data: PositionOperationRequest
    ) -> Dict[str, Any]:
        """
        执行持仓操作（加仓、减仓、分红、拆股、合股、调整成本）
        """
        # 查找该股票的现有持仓
        existing_position = await self.db[self.positions_collection].find_one({
            "user_id": user_id,
            "code": data.code,
            "market": data.market,
            "quantity": {"$gt": 0}
        })

        if not existing_position and data.operation_type != PositionOperationType.ADD:
            raise ValueError(f"未找到 {data.code} 的持仓记录")

        currency = self._get_currency_by_market(data.market)
        now = now_tz()

        if data.operation_type == PositionOperationType.ADD:
            # 加仓：复用 add_position 逻辑
            return await self._handle_add_operation(user_id, data, existing_position, currency, now)

        elif data.operation_type == PositionOperationType.REDUCE:
            # 减仓
            return await self._handle_reduce_operation(user_id, data, existing_position, currency, now)

        elif data.operation_type == PositionOperationType.DIVIDEND:
            # 分红
            return await self._handle_dividend_operation(user_id, data, existing_position, currency, now)

        elif data.operation_type == PositionOperationType.SPLIT:
            # 拆股
            return await self._handle_split_operation(user_id, data, existing_position, currency, now)

        elif data.operation_type == PositionOperationType.MERGE:
            # 合股
            return await self._handle_merge_operation(user_id, data, existing_position, currency, now)

        elif data.operation_type == PositionOperationType.ADJUST:
            # 调整成本价
            return await self._handle_adjust_operation(user_id, data, existing_position, currency, now)

        else:
            raise ValueError(f"不支持的操作类型: {data.operation_type}")

    async def _handle_add_operation(
        self, user_id: str, data: PositionOperationRequest,
        existing_position: Optional[Dict], currency: str, now
    ) -> Dict[str, Any]:
        """处理加仓操作"""
        if not data.quantity or not data.price:
            raise ValueError("加仓需要指定数量和价格")

        position_cost = data.quantity * data.price

        # 🔥 计算买入手续费
        market_rules = await self._get_real_market_rules(data.market)
        commission_info = self._calculate_real_commission(data.market, "buy", position_cost, market_rules or {})
        total_commission = commission_info["commission"]
        total_cost = position_cost + total_commission

        # 检查资金
        account = await self.get_or_create_account(user_id)
        current_cash = account.get("cash", {}).get(currency, 0.0)
        if current_cash < total_cost:
            raise ValueError(f"可用资金不足。当前: {current_cash:.2f}，需要: {total_cost:.2f}（含手续费 {total_commission:.2f}）")

        # 扣除资金（含手续费）
        await self.db[self.accounts_collection].update_one(
            {"user_id": user_id},
            {"$set": {f"cash.{currency}": current_cash - total_cost, "updated_at": now}}
        )

        if existing_position:
            # 已有持仓，合并（加仓时 cost_price 与 original_avg_cost 均为加权均价）
            old_qty = existing_position["quantity"]
            old_cost = existing_position["cost_price"]
            new_qty = old_qty + data.quantity
            new_cost = (old_qty * old_cost + position_cost) / new_qty

            await self.db[self.positions_collection].update_one(
                {"_id": existing_position["_id"]},
                {"$set": {
                    "quantity": new_qty,
                    "cost_price": round(new_cost, 4),
                    "original_avg_cost": round(new_cost, 4),
                    "updated_at": now
                }}
            )

            await self.record_position_change(
                user_id=user_id, change_type=PositionChangeType.BUY,
                code=data.code, name=existing_position.get("name", data.code),
                market=data.market, currency=currency,
                position_id=str(existing_position["_id"]),
                quantity_before=old_qty, cost_price_before=old_cost,
                quantity_after=new_qty, cost_price_after=new_cost,
                description=f"加仓: +{data.quantity}股 @ {data.price:.2f}",
                trade_time=data.operation_date  # 使用操作日期作为交易时间
            )

            logger.info(f"✅ 加仓成功: {data.code}, {old_qty} → {new_qty}股")
            return {"message": "加仓成功", "new_quantity": new_qty, "new_cost_price": round(new_cost, 4)}
        else:
            # 新建持仓
            name = await self._get_stock_name(data.code, data.market)
            position_doc = {
                "user_id": user_id, "code": data.code, "name": name,
                "market": data.market, "currency": currency,
                "quantity": data.quantity, "cost_price": data.price,
                "original_avg_cost": data.price,
                "buy_date": data.operation_date or now,
                "source": PositionSource.MANUAL.value,
                "created_at": now, "updated_at": now
            }
            result = await self.db[self.positions_collection].insert_one(position_doc)

            await self.record_position_change(
                user_id=user_id, change_type=PositionChangeType.BUY,
                code=data.code, name=name or data.code,
                market=data.market, currency=currency,
                position_id=str(result.inserted_id),
                quantity_before=0, cost_price_before=0.0,
                quantity_after=data.quantity, cost_price_after=data.price,
                description="新建持仓",
                trade_time=data.operation_date  # 使用操作日期作为交易时间
            )

            logger.info(f"✅ 新建持仓: {data.code}, {data.quantity}股 @ {data.price:.2f}")
            return {"message": "新建持仓成功", "new_quantity": data.quantity, "new_cost_price": data.price}

    async def _handle_reduce_operation(
        self, user_id: str, data: PositionOperationRequest,
        existing_position: Dict, currency: str, now
    ) -> Dict[str, Any]:
        """处理减仓操作"""
        if not data.quantity or not data.price:
            raise ValueError("减仓需要指定数量和价格")

        old_qty = existing_position["quantity"]
        old_cost = existing_position["cost_price"]

        if data.quantity > old_qty:
            raise ValueError(f"减仓数量({data.quantity})超过持仓数量({old_qty})")

        # 计算卖出金额
        sell_amount = data.quantity * data.price

        # 🔥 计算卖出手续费
        market_rules = await self._get_real_market_rules(data.market)
        commission_info = self._calculate_real_commission(data.market, "sell", sell_amount, market_rules or {})
        total_commission = commission_info["commission"]
        net_proceeds = sell_amount - total_commission  # 实际到账 = 卖出金额 - 手续费

        # 增加到现金（扣除手续费后）
        account = await self.get_or_create_account(user_id)
        current_cash = account.get("cash", {}).get(currency, 0.0)

        await self.db[self.accounts_collection].update_one(
            {"user_id": user_id},
            {"$set": {f"cash.{currency}": current_cash + net_proceeds, "updated_at": now}}
        )

        new_qty = old_qty - data.quantity
        old_total_cost = old_qty * old_cost

        if new_qty == 0:
            # 清仓
            await self.db[self.positions_collection].update_one(
                {"_id": existing_position["_id"]},
                {"$set": {"quantity": 0, "updated_at": now}}
            )
            change_type = PositionChangeType.SELL
            description = f"清仓: -{data.quantity}股 @ {data.price:.2f}"
            if total_commission > 0:
                description += f"（手续费 ¥{total_commission:.2f}）"
            new_cost = 0.0
        else:
            # 部分减仓：使用摊薄成本逻辑（与主流交易软件一致）
            # 剩余成本 = (原持仓总成本 - 本次卖出金额) / 剩余数量
            # 盈利减仓时成本降低，亏损减仓时成本升高
            remaining_cost = old_total_cost - sell_amount
            new_cost = remaining_cost / new_qty if new_qty > 0 else 0.0
            # 避免负成本（极端盈利时），保底为 0.01
            new_cost = max(0.01, round(new_cost, 4))

            await self.db[self.positions_collection].update_one(
                {"_id": existing_position["_id"]},
                {"$set": {"quantity": new_qty, "cost_price": new_cost, "updated_at": now}}
            )
            change_type = PositionChangeType.REDUCE
            description = f"减仓: -{data.quantity}股 @ {data.price:.2f}，成本 {old_cost:.2f} → {new_cost:.2f}"
            if total_commission > 0:
                description += f"（手续费 ¥{total_commission:.2f}）"

        # 计算盈亏（扣除手续费后的实际盈亏）
        profit = (data.price - old_cost) * data.quantity - total_commission
        profit_pct = (profit) / (old_cost * data.quantity) * 100 if old_cost > 0 else 0

        await self.record_position_change(
            user_id=user_id, change_type=change_type,
            code=data.code, name=existing_position.get("name", data.code),
            market=data.market, currency=currency,
            position_id=str(existing_position["_id"]),
            quantity_before=old_qty, cost_price_before=old_cost,
            quantity_after=new_qty, cost_price_after=new_cost,
            trade_price=data.price,
            realized_profit=profit,
            description=description,
            trade_time=data.operation_date,
            extra_data={"commission": commission_info} if total_commission > 0 else None
        )

        logger.info(
            f"✅ 减仓成功: {data.code}, {old_qty} → {new_qty}股, "
            f"成本 {old_cost:.2f} → {new_cost:.2f}, 盈亏: {profit:.2f}"
        )
        result = {
            "message": "清仓成功" if new_qty == 0 else "减仓成功",
            "new_quantity": new_qty,
            "sell_amount": round(sell_amount, 2),
            "net_proceeds": round(net_proceeds, 2),
            "commission": round(total_commission, 2),
            "profit": round(profit, 2),
            "profit_pct": round(profit_pct, 2),
        }
        if new_qty > 0:
            result["new_cost_price"] = round(new_cost, 4)
        return result

    async def _handle_dividend_operation(
        self, user_id: str, data: PositionOperationRequest,
        existing_position: Dict, currency: str, now
    ) -> Dict[str, Any]:
        """处理分红操作"""
        if not data.dividend_amount or data.dividend_amount <= 0:
            raise ValueError("分红需要指定分红金额")

        # 分红金额加入现金
        account = await self.get_or_create_account(user_id)
        current_cash = account.get("cash", {}).get(currency, 0.0)

        await self.db[self.accounts_collection].update_one(
            {"user_id": user_id},
            {"$set": {f"cash.{currency}": current_cash + data.dividend_amount, "updated_at": now}}
        )

        # 记录变动
        await self.record_position_change(
            user_id=user_id, change_type=PositionChangeType.ADJUST,
            code=data.code, name=existing_position.get("name", data.code),
            market=data.market, currency=currency,
            position_id=str(existing_position["_id"]),
            quantity_before=existing_position["quantity"],
            cost_price_before=existing_position["cost_price"],
            quantity_after=existing_position["quantity"],
            cost_price_after=existing_position["cost_price"],
            description=f"现金分红: +{data.dividend_amount:.2f} {currency}",
            trade_time=data.operation_date  # 使用操作日期作为交易时间
        )

        logger.info(f"✅ 分红成功: {data.code}, +{data.dividend_amount:.2f} {currency}")
        return {"message": "分红记录成功", "dividend_amount": data.dividend_amount}

    async def _handle_split_operation(
        self, user_id: str, data: PositionOperationRequest,
        existing_position: Dict, currency: str, now
    ) -> Dict[str, Any]:
        """处理拆股操作（如 10送10，比例为 2:1）"""
        if not data.ratio:
            raise ValueError("拆股需要指定比例，如 2:1")

        try:
            parts = data.ratio.split(":")
            new_ratio = float(parts[0])
            old_ratio = float(parts[1])
            multiplier = new_ratio / old_ratio
        except Exception:
            raise ValueError("比例格式错误，应为 x:y 格式，如 2:1")

        old_qty = existing_position["quantity"]
        old_cost = existing_position["cost_price"]

        new_qty = int(old_qty * multiplier)
        new_cost = old_cost / multiplier

        await self.db[self.positions_collection].update_one(
            {"_id": existing_position["_id"]},
            {"$set": {"quantity": new_qty, "cost_price": round(new_cost, 4), "updated_at": now}}
        )

        await self.record_position_change(
            user_id=user_id, change_type=PositionChangeType.ADJUST,
            code=data.code, name=existing_position.get("name", data.code),
            market=data.market, currency=currency,
            position_id=str(existing_position["_id"]),
            quantity_before=old_qty, cost_price_before=old_cost,
            quantity_after=new_qty, cost_price_after=new_cost,
            description=f"拆股: {data.ratio}，数量 {old_qty} → {new_qty}",
            trade_time=data.operation_date  # 使用操作日期作为交易时间
        )

        logger.info(f"✅ 拆股成功: {data.code}, {old_qty} → {new_qty}股")
        return {"message": "拆股成功", "new_quantity": new_qty, "new_cost_price": round(new_cost, 4)}

    async def _handle_merge_operation(
        self, user_id: str, data: PositionOperationRequest,
        existing_position: Dict, currency: str, now
    ) -> Dict[str, Any]:
        """处理合股操作（如 10合1，比例为 1:10）"""
        if not data.ratio:
            raise ValueError("合股需要指定比例，如 1:10")

        try:
            parts = data.ratio.split(":")
            new_ratio = float(parts[0])
            old_ratio = float(parts[1])
            multiplier = new_ratio / old_ratio
        except Exception:
            raise ValueError("比例格式错误，应为 x:y 格式，如 1:10")

        old_qty = existing_position["quantity"]
        old_cost = existing_position["cost_price"]

        new_qty = int(old_qty * multiplier)
        new_cost = old_cost / multiplier

        if new_qty < 1:
            raise ValueError(f"合股后数量({new_qty})不能小于1")

        await self.db[self.positions_collection].update_one(
            {"_id": existing_position["_id"]},
            {"$set": {"quantity": new_qty, "cost_price": round(new_cost, 4), "updated_at": now}}
        )

        await self.record_position_change(
            user_id=user_id, change_type=PositionChangeType.ADJUST,
            code=data.code, name=existing_position.get("name", data.code),
            market=data.market, currency=currency,
            position_id=str(existing_position["_id"]),
            quantity_before=old_qty, cost_price_before=old_cost,
            quantity_after=new_qty, cost_price_after=new_cost,
            description=f"合股: {data.ratio}，数量 {old_qty} → {new_qty}",
            trade_time=data.operation_date  # 使用操作日期作为交易时间
        )

        logger.info(f"✅ 合股成功: {data.code}, {old_qty} → {new_qty}股")
        return {"message": "合股成功", "new_quantity": new_qty, "new_cost_price": round(new_cost, 4)}

    async def _handle_adjust_operation(
        self, user_id: str, data: PositionOperationRequest,
        existing_position: Dict, currency: str, now
    ) -> Dict[str, Any]:
        """处理调整成本价操作"""
        if not data.new_cost_price or data.new_cost_price <= 0:
            raise ValueError("调整成本价需要指定新成本价")

        old_cost = existing_position["cost_price"]

        await self.db[self.positions_collection].update_one(
            {"_id": existing_position["_id"]},
            {"$set": {"cost_price": data.new_cost_price, "updated_at": now}}
        )

        await self.record_position_change(
            user_id=user_id, change_type=PositionChangeType.ADJUST,
            code=data.code, name=existing_position.get("name", data.code),
            market=data.market, currency=currency,
            position_id=str(existing_position["_id"]),
            quantity_before=existing_position["quantity"],
            cost_price_before=old_cost,
            quantity_after=existing_position["quantity"],
            cost_price_after=data.new_cost_price,
            description=f"调整成本价: {old_cost:.2f} → {data.new_cost_price:.2f}",
            trade_time=data.operation_date  # 使用操作日期作为交易时间
        )

        logger.info(f"✅ 调整成本价成功: {data.code}, {old_cost:.2f} → {data.new_cost_price:.2f}")
        return {"message": "调整成本价成功", "new_cost_price": data.new_cost_price}

    async def update_position(
        self,
        user_id: str,
        position_id: str,
        data: PositionUpdate
    ) -> Optional[PositionResponse]:
        """更新持仓"""
        try:
            obj_id = ObjectId(position_id)
        except Exception:
            logger.error(f"无效的持仓ID: {position_id}")
            return None

        # 获取原持仓信息
        old_position = await self.db[self.positions_collection].find_one(
            {"_id": obj_id, "user_id": user_id}
        )
        if not old_position:
            return None

        # 计算资金变化
        old_cost = old_position["quantity"] * old_position["cost_price"]
        new_quantity = data.quantity if data.quantity is not None else old_position["quantity"]
        new_cost_price = data.cost_price if data.cost_price is not None else old_position["cost_price"]
        new_cost = new_quantity * new_cost_price
        cost_diff = new_cost - old_cost  # 正数表示需要追加资金，负数表示释放资金

        # 更新资金账户
        if cost_diff != 0:
            currency = old_position["currency"]
            account = await self.get_or_create_account(user_id)
            current_cash = account.get("cash", {}).get(currency, 0.0)

            if cost_diff > 0 and current_cash < cost_diff:
                raise ValueError(f"可用资金不足。当前可用: {current_cash:.2f} {currency}，需要追加: {cost_diff:.2f} {currency}")

            new_cash = current_cash - cost_diff
            await self.db[self.accounts_collection].update_one(
                {"user_id": user_id},
                {"$set": {f"cash.{currency}": new_cash, "updated_at": now_tz()}}
            )

        # 构建更新数据
        update_data = {"updated_at": now_tz()}
        if data.name is not None:
            update_data["name"] = data.name
        if data.quantity is not None:
            update_data["quantity"] = data.quantity
        if data.cost_price is not None:
            update_data["cost_price"] = data.cost_price
        if data.buy_date is not None:
            update_data["buy_date"] = data.buy_date
        if data.notes is not None:
            update_data["notes"] = data.notes

        result = await self.db[self.positions_collection].find_one_and_update(
            {"_id": obj_id, "user_id": user_id},
            {"$set": update_data},
            return_document=True
        )

        if result:
            # 记录持仓变动
            currency = old_position["currency"]
            old_qty = old_position["quantity"]
            old_price = old_position["cost_price"]

            # 判断变动类型
            if new_quantity > old_qty:
                change_type = PositionChangeType.ADD
                desc = f"加仓 {new_quantity - old_qty} 股"
            elif new_quantity < old_qty:
                change_type = PositionChangeType.REDUCE
                desc = f"减仓 {old_qty - new_quantity} 股"
            else:
                change_type = PositionChangeType.ADJUST
                desc = "调整持仓信息"

            await self.record_position_change(
                user_id=user_id,
                change_type=change_type,
                code=old_position["code"],
                name=result.get("name", old_position["code"]),
                market=old_position["market"],
                currency=currency,
                position_id=position_id,
                quantity_before=old_qty,
                cost_price_before=old_price,
                quantity_after=new_quantity,
                cost_price_after=new_cost_price,
                description=desc
            )

            logger.info(f"✅ 更新持仓成功: {position_id}，资金变化: {-cost_diff:.2f} {currency}")
            return await self._enrich_position(result, "real")
        return None

    async def delete_position(self, user_id: str, position_id: str) -> bool:
        """删除持仓"""
        try:
            obj_id = ObjectId(position_id)
        except Exception:
            logger.error(f"无效的持仓ID: {position_id}")
            return False

        # 获取持仓信息以释放资金
        position = await self.db[self.positions_collection].find_one(
            {"_id": obj_id, "user_id": user_id}
        )
        if not position:
            return False

        # 释放资金（将持仓成本返还到可用资金）
        position_cost = position["quantity"] * position["cost_price"]
        currency = position["currency"]

        account = await self.get_or_create_account(user_id)
        current_cash = account.get("cash", {}).get(currency, 0.0)
        new_cash = current_cash + position_cost

        await self.db[self.accounts_collection].update_one(
            {"user_id": user_id},
            {"$set": {f"cash.{currency}": new_cash, "updated_at": now_tz()}}
        )

        # 记录持仓变动（在删除前记录）
        await self.record_position_change(
            user_id=user_id,
            change_type=PositionChangeType.SELL,
            code=position["code"],
            name=position.get("name", position["code"]),
            market=position["market"],
            currency=currency,
            position_id=position_id,
            quantity_before=position["quantity"],
            cost_price_before=position["cost_price"],
            quantity_after=0,
            cost_price_after=0.0,
            description="清仓卖出"
        )

        # 删除持仓
        result = await self.db[self.positions_collection].delete_one(
            {"_id": obj_id, "user_id": user_id}
        )

        if result.deleted_count > 0:
            logger.info(f"✅ 删除持仓成功: {position_id}，释放资金: {position_cost:.2f} {currency}")
            return True
        return False

    async def import_positions(
        self,
        user_id: str,
        positions: List[PositionCreate],
        validate_input: bool = True,
        replace_existing: bool = False,
    ) -> Dict[str, Any]:
        """批量导入持仓"""
        sync_result: Optional[Dict[str, Any]] = None
        if replace_existing:
            sync_result = await self._clear_existing_real_positions_for_sync(user_id)

        success_count = 0
        failed_count = 0
        errors = []

        for pos in positions:
            try:
                await self.add_position(
                    user_id,
                    pos,
                    source=PositionSource.IMPORT,
                    validate_input=validate_input,
                )
                success_count += 1
            except Exception as e:
                failed_count += 1
                errors.append({"code": pos.code, "error": str(e)})

        logger.info(f"✅ 批量导入完成: 成功 {success_count}, 失败 {failed_count}")
        result = {
            "success_count": success_count,
            "failed_count": failed_count,
            "errors": errors
        }
        if sync_result:
            result.update(sync_result)
        result["replace_existing"] = replace_existing
        return result

    async def import_positions_from_qmt(
        self,
        user_id: str,
        account_id: str,
        account_type: str = "STOCK",
    ) -> Dict[str, Any]:
        """从 QMT 只读查询持仓并导入到当前用户的持仓列表。"""
        from app.services.data_sources.qmt_adapter import QMTAdapter

        adapter = QMTAdapter()
        snapshot = await asyncio.to_thread(adapter.query_account_snapshot, account_id, account_type)
        if snapshot is None:
            raise ValueError("QMT 未查询到账户信息，请确认 QMT 已登录且账号可用")

        positions_df = snapshot.get("positions_df")
        asset_summary = snapshot.get("asset") or {}
        positions = self._build_qmt_import_positions(positions_df)

        if (positions_df is None or len(positions_df) == 0) and asset_summary.get("cash") is None:
            raise ValueError("QMT 未查询到可同步的持仓或账户资产")

        result = await self.import_positions(
            user_id,
            positions,
            validate_input=False,
            replace_existing=True,
        )
        result.update(await self._sync_qmt_cash_to_account(user_id, asset_summary))
        result.update({
            "account_id": account_id,
            "account_type": account_type,
            "queried_count": 0 if positions_df is None else len(positions_df),
            "imported_count": len(positions),
            "qmt_market_value": asset_summary.get("market_value"),
            "qmt_total_asset": asset_summary.get("total_asset"),
            "sync_note": "已覆盖本地实盘持仓，并将 CNY 可用现金同步为 QMT 账户现金。初始资金和累计入出金记录未改动。",
        })
        return result
