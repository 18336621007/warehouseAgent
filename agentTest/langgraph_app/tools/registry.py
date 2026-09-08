# 统一工具注册表：工具成为一等公民，与节点解耦；节点按组取工具，不再硬查工具名。
# 安全元数据（只读/行数上限/白名单）在这里集中声明，为 M2 Planner 工具化铺路。
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple


@dataclass
class ToolSecurity:
    """工具安全元数据：只读 / 行数上限 / 是否仅限白名单表。

    M1 仅做声明，校验逻辑仍在上层（validate_sql_node / sql_safety_validator /
    execute_sql_node）执行，后续阶段再逐步收敛到安全层。
    """
    read_only: bool = True
    row_limit: int = 0  # 0 表示不限制（由上层 SQL 校验兜底）
    whitelist_only: bool = True  # 是否只允许访问白名单内的表


@dataclass
class ToolSpec:
    """工具的注册描述：名称 / 描述 / 实例 / 所属组 / 安全元数据 / 参数 schema。"""
    name: str
    description: str
    tool: Any
    groups: Tuple[str, ...] = ("common",)  # advisor / seeker / common
    security: ToolSecurity = field(default_factory=ToolSecurity)
    args_schema: Any = None  # 可选参数 schema（如 pydantic BaseModel）


class ToolRegistry:
    """工具注册与按组取用中心；新增工具只注册一次，各节点按需取子集。"""

    def __init__(self):
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        # 工具名全局唯一，重复注册直接报错，避免静默覆盖
        if spec.name in self._specs:
            raise ValueError(f"工具重复注册: {spec.name}")
        self._specs[spec.name] = spec

    def get_by_name(self, name: str) -> ToolSpec:
        # 按工具名取注册描述（含安全元数据），未注册时报错
        if name not in self._specs:
            raise KeyError(f"工具未注册: {name}")
        return self._specs[name]

    def get(self, group: Optional[str] = None) -> List[Any]:
        """按组返回工具实例列表；group=None 返回全部。"""
        if group is None:
            return [spec.tool for spec in self._specs.values()]
        return [spec.tool for spec in self._specs.values() if group in spec.groups]
