"""PortfolioService.PositionChangeMixin。"""
from app.services.portfolio.support import (
    datetime,
    Dict,
    Any,
    List,
    ObjectId,
    PositionSource,
    PositionChangeType,
    PositionChangeResponse,
    now_tz,
    logger,
)

class PositionChangeMixin:

    # ==================== 持仓变动记录 ====================

    async def record_position_change(
        self,
        user_id: str,
        change_type: PositionChangeType,
        code: str,
        name: str,
        market: str,
        currency: str,
        position_id: str = None,
        quantity_before: int = 0,
        cost_price_before: float = 0.0,
        quantity_after: int = 0,
        cost_price_after: float = 0.0,
        trade_price: float = None,
        realized_profit: float = None,
        description: str = None,
        trade_time: datetime = None,
        extra_data: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """记录持仓变动

        Args:
            trade_time: 交易时间（实际交易发生的时间，由用户手工录入）
                       如果不提供，则使用当前时间
            extra_data: 额外数据（如手续费明细）
        """
        cost_value_before = quantity_before * cost_price_before
        cost_value_after = quantity_after * cost_price_after
        quantity_change = quantity_after - quantity_before
        cash_change = cost_value_before - cost_value_after  # 正数表示释放资金

        change_doc = {
            "user_id": user_id,
            "position_id": position_id,
            "code": code,
            "name": name,
            "market": market,
            "currency": currency,
            "change_type": change_type.value,
            "quantity_before": quantity_before,
            "cost_price_before": cost_price_before,
            "cost_value_before": round(cost_value_before, 2),
            "quantity_after": quantity_after,
            "cost_price_after": cost_price_after,
            "cost_value_after": round(cost_value_after, 2),
            "quantity_change": quantity_change,
            "cash_change": round(cash_change, 2),
            "trade_price": trade_price,
            "realized_profit": round(realized_profit, 2) if realized_profit else None,
            "description": description,
            "trade_time": trade_time or now_tz(),  # 交易时间，如果不提供则使用当前时间
            "created_at": now_tz()  # 记录创建时间
        }

        # 合并额外数据（如手续费明细）
        if extra_data:
            change_doc.update(extra_data)

        result = await self.db[self.position_changes_collection].insert_one(change_doc)
        change_doc["_id"] = result.inserted_id

        logger.info(f"📝 记录持仓变动: {user_id} - {code} - {change_type.value}")
        return change_doc

    async def get_position_changes(
        self,
        user_id: str,
        code: str = None,
        market: str = None,
        change_type: str = None,
        limit: int = 100,
        skip: int = 0
    ) -> List[PositionChangeResponse]:
        """获取持仓变动记录"""
        query = {"user_id": user_id}
        if code:
            query["code"] = code
        if market:
            query["market"] = market
        if change_type:
            query["change_type"] = change_type

        cursor = self.db[self.position_changes_collection].find(query).sort("created_at", -1).skip(skip).limit(limit)
        changes = await cursor.to_list(None)

        result = []
        for c in changes:
            result.append(PositionChangeResponse(
                id=str(c["_id"]),
                position_id=c.get("position_id"),
                code=c["code"],
                name=c["name"],
                market=c["market"],
                currency=c.get("currency", "CNY"),
                change_type=c["change_type"],
                quantity_before=c.get("quantity_before", 0),
                cost_price_before=c.get("cost_price_before", 0.0),
                cost_value_before=c.get("cost_value_before", 0.0),
                quantity_after=c.get("quantity_after", 0),
                cost_price_after=c.get("cost_price_after", 0.0),
                cost_value_after=c.get("cost_value_after", 0.0),
                quantity_change=c.get("quantity_change", 0),
                cash_change=c.get("cash_change", 0.0),
                trade_price=c.get("trade_price"),
                realized_profit=c.get("realized_profit"),
                description=c.get("description"),
                trade_time=c.get("trade_time"),  # 交易时间
                created_at=c["created_at"]
            ))

        return result

    async def get_position_changes_count(
        self,
        user_id: str,
        code: str = None,
        market: str = None,
        change_type: str = None
    ) -> int:
        """获取持仓变动记录总数"""
        query = {"user_id": user_id}
        if code:
            query["code"] = code
        if market:
            query["market"] = market
        if change_type:
            query["change_type"] = change_type

        return await self.db[self.position_changes_collection].count_documents(query)

    async def update_position_change(
        self,
        user_id: str,
        change_id: str,
        quantity: int,
        price: float,
        trade_time: datetime = None
    ) -> Dict[str, Any]:
        """
        修改单笔交易记录（用于修正录入错误：数量、单价）
        支持 buy/add/reduce/sell 类型，修改后会级联重算后续记录并更新持仓
        """
        from bson import ObjectId
        try:
            obj_id = ObjectId(change_id)
        except Exception:
            raise ValueError("无效的变动记录ID")

        change = await self.db[self.position_changes_collection].find_one(
            {"_id": obj_id, "user_id": user_id}
        )
        if not change:
            raise ValueError("变动记录不存在或无权修改")

        ct = change.get("change_type")
        if ct not in ("buy", "add", "reduce", "sell"):
            raise ValueError("仅支持修改买入、加仓、减仓、卖出记录")

        code = change["code"]
        name = change.get("name", code)
        market = change.get("market", "CN")
        currency = change.get("currency", "CNY")

        # 获取该股票所有变动记录，按时间排序
        cursor = self.db[self.position_changes_collection].find(
            {"user_id": user_id, "code": code, "market": market}
        ).sort("created_at", 1)
        all_changes = await cursor.to_list(None)

        edit_idx = None
        for i, c in enumerate(all_changes):
            if c["_id"] == obj_id:
                edit_idx = i
                break
        if edit_idx is None:
            raise ValueError("变动记录不存在")

        # 计算编辑前的状态（前一笔的 quantity_after, cost_value_after）
        prev_qty = 0
        prev_cost_val = 0.0
        if edit_idx > 0:
            prev = all_changes[edit_idx - 1]
            prev_qty = prev.get("quantity_after", 0)
            prev_cost_val = prev.get("cost_value_after", 0.0)

        # 根据类型计算编辑后的记录
        def compute_updated_change(c, qty_before, cost_val_before, new_qty, new_price):
            ct = c.get("change_type")
            cost_val_before = round(cost_val_before, 2)
            cost_price_before = round(cost_val_before / qty_before, 4) if qty_before > 0 else 0.0

            if ct in ("buy", "add"):
                qty_after = qty_before + new_qty
                cost_val_after = round(qty_before * cost_price_before + new_qty * new_price, 2)
                cost_price_after = round(cost_val_after / qty_after, 4) if qty_after > 0 else 0.0
                cost_val_after = round(qty_after * cost_price_after, 2)
                cash_change = round(cost_val_before - cost_val_after, 2)
                return {
                    "quantity_before": qty_before,
                    "cost_price_before": cost_price_before,
                    "cost_value_before": cost_val_before,
                    "quantity_after": qty_after,
                    "cost_price_after": cost_price_after,
                    "cost_value_after": cost_val_after,
                    "quantity_change": new_qty,
                    "cash_change": cash_change,
                    "trade_price": None,
                    "realized_profit": None,
                }
            else:  # reduce, sell
                qty_after = qty_before - new_qty
                cost_per_share = cost_val_before / qty_before if qty_before > 0 else 0.0
                cost_val_after = round(qty_after * cost_per_share, 2) if qty_after > 0 else 0.0
                cost_price_after = round(cost_val_after / qty_after, 4) if qty_after > 0 else 0.0
                realized = round(new_qty * (new_price - cost_per_share), 2)
                cash_change = round(cost_val_before - cost_val_after + new_qty * new_price, 2)
                return {
                    "quantity_before": qty_before,
                    "cost_price_before": cost_price_before,
                    "cost_value_before": cost_val_before,
                    "quantity_after": qty_after,
                    "cost_price_after": cost_price_after,
                    "cost_value_after": cost_val_after,
                    "quantity_change": -new_qty,
                    "cash_change": cash_change,
                    "trade_price": new_price,
                    "realized_profit": realized,
                }

        # 更新编辑的记录
        updated = compute_updated_change(change, prev_qty, prev_cost_val, quantity, price)
        update_doc = {**updated}
        if trade_time:
            update_doc["trade_time"] = trade_time
        await self.db[self.position_changes_collection].update_one(
            {"_id": obj_id, "user_id": user_id},
            {"$set": update_doc}
        )

        # 级联更新后续记录（用新的 curr 状态重算每笔的派生字段）
        curr_qty = updated["quantity_after"]
        curr_cost_val = updated["cost_value_after"]
        for i in range(edit_idx + 1, len(all_changes)):
            c = all_changes[i]
            cid = c["_id"]
            ct = c.get("change_type")
            if ct in ("buy", "add"):
                qty_change = c.get("quantity_change", 0)
                orig_qty_after = c.get("quantity_after", 0)
                orig_cost_after = c.get("cost_price_after", 0)
                unit_price = (orig_cost_after * orig_qty_after - curr_cost_val) / qty_change if qty_change > 0 else 0
                cascade_updated = compute_updated_change(c, curr_qty, curr_cost_val, qty_change, unit_price)
            else:  # reduce, sell
                qty_sold = abs(c.get("quantity_change", 0))
                trade_p = c.get("trade_price", 0)
                cascade_updated = compute_updated_change(c, curr_qty, curr_cost_val, qty_sold, trade_p)
            await self.db[self.position_changes_collection].update_one(
                {"_id": cid, "user_id": user_id},
                {"$set": cascade_updated}
            )
            curr_qty = cascade_updated["quantity_after"]
            curr_cost_val = cascade_updated["cost_value_after"]

        cost_price = round(curr_cost_val / curr_qty, 4) if curr_qty > 0 else 0.0

        # 更新 positions 表
        position = await self.db[self.positions_collection].find_one(
            {"user_id": user_id, "code": code, "market": market}
        )
        if position:
            await self.db[self.positions_collection].update_one(
                {"_id": position["_id"]},
                {"$set": {
                    "quantity": curr_qty,
                    "cost_price": cost_price,
                    "original_avg_cost": cost_price,
                    "updated_at": now_tz()
                }}
            )

        logger.info(f"单笔记录修正: {code} 变动记录 {change_id}, 数量 {quantity}, 单价 {price}")
        return {"message": "记录修正成功", "quantity": curr_qty, "cost_price": cost_price}

    async def delete_position_change(
        self,
        user_id: str,
        change_id: str
    ) -> Dict[str, Any]:
        """
        删除单笔变动记录，级联重算后续记录并更新持仓
        """
        from bson import ObjectId
        try:
            obj_id = ObjectId(change_id)
        except Exception:
            raise ValueError("无效的变动记录ID")

        change = await self.db[self.position_changes_collection].find_one(
            {"_id": obj_id, "user_id": user_id}
        )
        if not change:
            raise ValueError("变动记录不存在或无权删除")

        code = change["code"]
        market = change.get("market", "CN")

        # 获取该股票所有变动记录，按时间排序，排除要删除的
        cursor = self.db[self.position_changes_collection].find(
            {"user_id": user_id, "code": code, "market": market}
        ).sort("created_at", 1)
        all_changes = await cursor.to_list(None)
        remaining = [c for c in all_changes if c["_id"] != obj_id]

        # 删除该记录
        await self.db[self.position_changes_collection].delete_one(
            {"_id": obj_id, "user_id": user_id}
        )

        # 重放剩余记录，级联更新每条
        def compute_updated_change(c, qty_before, cost_val_before, new_qty, new_price):
            ct = c.get("change_type")
            cost_val_before = round(cost_val_before, 2)
            cost_price_before = round(cost_val_before / qty_before, 4) if qty_before > 0 else 0.0
            if ct in ("buy", "add"):
                qty_after = qty_before + new_qty
                cost_val_after = round(qty_before * cost_price_before + new_qty * new_price, 2)
                cost_price_after = round(cost_val_after / qty_after, 4) if qty_after > 0 else 0.0
                cost_val_after = round(qty_after * cost_price_after, 2)
                cash_change = round(cost_val_before - cost_val_after, 2)
                return {
                    "quantity_before": qty_before, "cost_price_before": cost_price_before,
                    "cost_value_before": cost_val_before, "quantity_after": qty_after,
                    "cost_price_after": cost_price_after, "cost_value_after": cost_val_after,
                    "quantity_change": new_qty, "cash_change": cash_change,
                    "trade_price": None, "realized_profit": None,
                }
            else:
                qty_after = qty_before - new_qty
                cost_per_share = cost_val_before / qty_before if qty_before > 0 else 0.0
                cost_val_after = round(qty_after * cost_per_share, 2) if qty_after > 0 else 0.0
                cost_price_after = round(cost_val_after / qty_after, 4) if qty_after > 0 else 0.0
                realized = round(new_qty * (new_price - cost_per_share), 2)
                cash_change = round(cost_val_before - cost_val_after + new_qty * new_price, 2)
                return {
                    "quantity_before": qty_before, "cost_price_before": cost_price_before,
                    "cost_value_before": cost_val_before, "quantity_after": qty_after,
                    "cost_price_after": cost_price_after, "cost_value_after": cost_val_after,
                    "quantity_change": -new_qty, "cash_change": cash_change,
                    "trade_price": new_price, "realized_profit": realized,
                }

        curr_qty = 0
        curr_cost_val = 0.0
        for c in remaining:
            cid = c["_id"]
            ct = c.get("change_type")
            if ct in ("buy", "add"):
                qty_change = c.get("quantity_change", 0)
                orig_qty_after = c.get("quantity_after", 0)
                orig_cost_after = c.get("cost_price_after", 0)
                unit_price = (orig_cost_after * orig_qty_after - curr_cost_val) / qty_change if qty_change > 0 else 0
                updated = compute_updated_change(c, curr_qty, curr_cost_val, qty_change, unit_price)
            else:
                qty_sold = abs(c.get("quantity_change", 0))
                trade_p = c.get("trade_price", 0)
                updated = compute_updated_change(c, curr_qty, curr_cost_val, qty_sold, trade_p)
            await self.db[self.position_changes_collection].update_one(
                {"_id": cid, "user_id": user_id},
                {"$set": updated}
            )
            curr_qty = updated["quantity_after"]
            curr_cost_val = updated["cost_value_after"]

        cost_price = round(curr_cost_val / curr_qty, 4) if curr_qty > 0 else 0.0

        position = await self.db[self.positions_collection].find_one(
            {"user_id": user_id, "code": code, "market": market}
        )
        if position:
            await self.db[self.positions_collection].update_one(
                {"_id": position["_id"]},
                {"$set": {
                    "quantity": curr_qty,
                    "cost_price": cost_price,
                    "original_avg_cost": cost_price,
                    "updated_at": now_tz()
                }}
            )
        elif curr_qty > 0:
            # 有变动记录但无持仓，需新建
            name = change.get("name", code)
            currency = change.get("currency", "CNY")
            now = now_tz()
            await self.db[self.positions_collection].insert_one({
                "user_id": user_id, "code": code, "name": name, "market": market,
                "currency": currency, "quantity": curr_qty, "cost_price": cost_price,
                "original_avg_cost": cost_price, "source": PositionSource.MANUAL.value,
                "created_at": now, "updated_at": now
            })

        logger.info(f"删除变动记录: {code} {change_id}")
        return {"message": "删除成功", "quantity": curr_qty, "cost_price": cost_price}

    async def reset_position(self, user_id: str, code: str, market: str = "CN") -> Dict[str, Any]:
        """
        重置整个持仓：删除该股票的所有变动记录和持仓，资金不返还（用户重新录入）
        """
        position = await self.db[self.positions_collection].find_one(
            {"user_id": user_id, "code": code, "market": market}
        )
        deleted_changes = await self.db[self.position_changes_collection].delete_many(
            {"user_id": user_id, "code": code, "market": market}
        )
        result = await self.db[self.positions_collection].delete_one(
            {"user_id": user_id, "code": code, "market": market}
        )
        logger.info(f"重置持仓: {code} {market}, 删除 {deleted_changes.deleted_count} 条变动, 持仓 {'已删' if result.deleted_count else '不存在'}")
        return {
            "message": "重置成功",
            "deleted_changes": deleted_changes.deleted_count,
            "position_deleted": result.deleted_count > 0
        }

    async def reset_all_positions(self, user_id: str) -> Dict[str, Any]:
        """
        清零全部持仓：删除该用户所有持仓和变动记录，并完全清零资金账户
        资金账户全部归零：现金、初始资金、入金、出金、累计收益均为 0
        """
        deleted_changes = await self.db[self.position_changes_collection].delete_many(
            {"user_id": user_id}
        )
        deleted_positions = await self.db[self.positions_collection].delete_many(
            {"user_id": user_id}
        )

        # 完全清零资金账户：现金、初始资金、入金、出金全部置为 0
        zero_balance = {"CNY": 0.0, "HKD": 0.0, "USD": 0.0}
        now = now_tz()
        await self.db[self.accounts_collection].update_one(
            {"user_id": user_id},
            {"$set": {
                "cash": zero_balance,
                "initial_capital": zero_balance,
                "total_deposit": zero_balance,
                "total_withdraw": zero_balance,
                "updated_at": now
            }}
        )

        logger.info(f"清零全部持仓: user={user_id}, 删除 {deleted_changes.deleted_count} 条变动, {deleted_positions.deleted_count} 个持仓, 资金账户已完全清零")
        return {
            "message": "清零成功",
            "deleted_changes": deleted_changes.deleted_count,
            "deleted_positions": deleted_positions.deleted_count
        }
