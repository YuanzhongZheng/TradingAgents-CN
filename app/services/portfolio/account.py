"""PortfolioService.AccountMixin。"""
from app.services.portfolio.support import (
    Dict,
    Any,
    List,
    ObjectId,
    CapitalTransactionType,
    AccountSettingsRequest,
    AccountSummary,
    now_tz,
)

class AccountMixin:

    # ==================== 资金账户管理 ====================

    async def get_or_create_account(self, user_id: str) -> Dict[str, Any]:
        """获取或创建资金账户"""
        acc = await self.db[self.accounts_collection].find_one({"user_id": user_id})
        if not acc:
            now = now_tz()
            acc = {
                "user_id": user_id,
                "cash": {"CNY": 0.0, "HKD": 0.0, "USD": 0.0},
                "initial_capital": {"CNY": 0.0, "HKD": 0.0, "USD": 0.0},
                "total_deposit": {"CNY": 0.0, "HKD": 0.0, "USD": 0.0},
                "total_withdraw": {"CNY": 0.0, "HKD": 0.0, "USD": 0.0},
                "settings": {
                    "default_market": "CN",
                    "max_position_pct": 30.0,
                    "max_loss_pct": 10.0
                },
                "created_at": now,
                "updated_at": now
            }
            await self.db[self.accounts_collection].insert_one(acc)
        return acc

    async def initialize_account(
        self, user_id: str, initial_capital: float, currency: str = "CNY"
    ) -> Dict[str, Any]:
        """初始化资金账户（设置初始资金）"""
        acc = await self.get_or_create_account(user_id)
        now = now_tz()

        # 获取当前余额
        current_cash = acc.get("cash", {}).get(currency, 0.0)
        new_cash = current_cash + initial_capital

        # 更新账户
        update_data = {
            f"cash.{currency}": new_cash,
            f"initial_capital.{currency}": acc.get("initial_capital", {}).get(currency, 0.0) + initial_capital,
            "updated_at": now
        }
        await self.db[self.accounts_collection].update_one(
            {"user_id": user_id},
            {"$set": update_data}
        )

        # 记录交易
        transaction = {
            "user_id": user_id,
            "transaction_type": CapitalTransactionType.INITIAL.value,
            "amount": initial_capital,
            "currency": currency,
            "balance_before": current_cash,
            "balance_after": new_cash,
            "description": f"初始资金 {initial_capital:,.2f} {currency}",
            "created_at": now
        }
        await self.db[self.transactions_collection].insert_one(transaction)

        return await self.get_or_create_account(user_id)

    async def deposit(
        self, user_id: str, amount: float, currency: str = "CNY", description: str = None
    ) -> Dict[str, Any]:
        """入金"""
        acc = await self.get_or_create_account(user_id)
        now = now_tz()

        current_cash = acc.get("cash", {}).get(currency, 0.0)
        new_cash = current_cash + amount
        current_deposit = acc.get("total_deposit", {}).get(currency, 0.0)

        update_data = {
            f"cash.{currency}": new_cash,
            f"total_deposit.{currency}": current_deposit + amount,
            "updated_at": now
        }
        await self.db[self.accounts_collection].update_one(
            {"user_id": user_id},
            {"$set": update_data}
        )

        transaction = {
            "user_id": user_id,
            "transaction_type": CapitalTransactionType.DEPOSIT.value,
            "amount": amount,
            "currency": currency,
            "balance_before": current_cash,
            "balance_after": new_cash,
            "description": description or f"入金 {amount:,.2f} {currency}",
            "created_at": now
        }
        await self.db[self.transactions_collection].insert_one(transaction)

        return await self.get_or_create_account(user_id)

    async def withdraw(
        self, user_id: str, amount: float, currency: str = "CNY", description: str = None
    ) -> Dict[str, Any]:
        """出金"""
        acc = await self.get_or_create_account(user_id)
        now = now_tz()

        current_cash = acc.get("cash", {}).get(currency, 0.0)
        if current_cash < amount:
            raise ValueError(f"可用{currency}不足：需要 {amount:.2f}，可用 {current_cash:.2f}")

        new_cash = current_cash - amount
        current_withdraw = acc.get("total_withdraw", {}).get(currency, 0.0)

        update_data = {
            f"cash.{currency}": new_cash,
            f"total_withdraw.{currency}": current_withdraw + amount,
            "updated_at": now
        }
        await self.db[self.accounts_collection].update_one(
            {"user_id": user_id},
            {"$set": update_data}
        )

        transaction = {
            "user_id": user_id,
            "transaction_type": CapitalTransactionType.WITHDRAW.value,
            "amount": -amount,  # 出金为负数
            "currency": currency,
            "balance_before": current_cash,
            "balance_after": new_cash,
            "description": description or f"出金 {amount:,.2f} {currency}",
            "created_at": now
        }
        await self.db[self.transactions_collection].insert_one(transaction)

        return await self.get_or_create_account(user_id)

    async def update_account_settings(
        self, user_id: str, settings: AccountSettingsRequest
    ) -> Dict[str, Any]:
        """更新账户设置"""
        acc = await self.get_or_create_account(user_id)
        now = now_tz()

        update_data = {"updated_at": now}
        if settings.max_position_pct is not None:
            update_data["settings.max_position_pct"] = settings.max_position_pct
        if settings.max_loss_pct is not None:
            update_data["settings.max_loss_pct"] = settings.max_loss_pct
        if settings.default_market is not None:
            update_data["settings.default_market"] = settings.default_market

        await self.db[self.accounts_collection].update_one(
            {"user_id": user_id},
            {"$set": update_data}
        )

        return await self.get_or_create_account(user_id)

    async def get_account_summary(self, user_id: str) -> AccountSummary:
        """获取账户摘要（含持仓市值计算）"""
        acc = await self.get_or_create_account(user_id)

        # 获取持仓市值（只计算用户持仓，排除模拟盘）
        positions = await self.get_positions(user_id, include_market_data=True)
        positions_value = {"CNY": 0.0, "HKD": 0.0, "USD": 0.0}
        for pos in positions:
            # 只计算非模拟盘的持仓
            if pos.source == "paper":
                continue
            currency = pos.currency or "CNY"
            positions_value[currency] = positions_value.get(currency, 0.0) + (pos.market_value or 0.0)

        # 计算各项指标
        cash = acc.get("cash", {"CNY": 0.0, "HKD": 0.0, "USD": 0.0})
        initial_capital = acc.get("initial_capital", {"CNY": 0.0, "HKD": 0.0, "USD": 0.0})
        total_deposit = acc.get("total_deposit", {"CNY": 0.0, "HKD": 0.0, "USD": 0.0})
        total_withdraw = acc.get("total_withdraw", {"CNY": 0.0, "HKD": 0.0, "USD": 0.0})

        net_capital = {}
        total_assets = {}
        profit = {}
        profit_pct = {}

        for currency in ["CNY", "HKD", "USD"]:
            # 净入金 = 初始 + 入金 - 出金
            net = (initial_capital.get(currency, 0.0) +
                   total_deposit.get(currency, 0.0) -
                   total_withdraw.get(currency, 0.0))
            net_capital[currency] = round(net, 2)

            # 总资产 = 现金 + 持仓市值
            assets = cash.get(currency, 0.0) + positions_value.get(currency, 0.0)
            total_assets[currency] = round(assets, 2)

            # 收益 = 总资产 - 净入金
            pnl = assets - net
            profit[currency] = round(pnl, 2)

            # 收益率
            if net > 0:
                profit_pct[currency] = round((pnl / net) * 100, 2)
            else:
                profit_pct[currency] = 0.0

        return AccountSummary(
            cash=cash,
            initial_capital=initial_capital,
            total_deposit=total_deposit,
            total_withdraw=total_withdraw,
            net_capital=net_capital,
            positions_value=positions_value,
            total_assets=total_assets,
            profit=profit,
            profit_pct=profit_pct,
            settings=acc.get("settings", {})
        )

    async def get_transactions(
        self, user_id: str, currency: str = None, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """获取资金交易记录"""
        query = {"user_id": user_id}
        if currency:
            query["currency"] = currency

        cursor = self.db[self.transactions_collection].find(query).sort("created_at", -1).limit(limit)
        transactions = await cursor.to_list(None)

        # 转换 ObjectId
        for t in transactions:
            t["id"] = str(t.pop("_id"))

        return transactions

    async def get_total_capital(self, user_id: str, currency: str = "CNY") -> float:
        """获取用户的总资金（现金 + 持仓市值），用于持仓分析"""
        summary = await self.get_account_summary(user_id)
        return summary.total_assets.get(currency, 0.0)
