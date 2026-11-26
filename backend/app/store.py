"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """恢复初始示例数据；验收脚本可用它隔离不同场景。"""
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [deepcopy(row) for row in rows] for name, rows in SEED_ROWS.items()
        }

    # 直观别名，兼容不同验收脚本的命名。
    reset_data = reset

    def module_names(self) -> list[str]:
        # 业务模块固定为 SEED_ROWS 中的 20 个；merge_lineage 等是内部台账，
        # 不应混入健康检查模块数和运营概览卡片。
        return sorted(SEED_ROWS.keys())

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
