"""基于 z3 的小样本格值暴露风险审查。

审查模型（不依赖真实检出数作为约束）：
- 每个格一个未知整数 x_i，仅受 0 <= x_i <= 人数_i 界限约束；
- 每条历次公开记录：所选交集格之和 == 公开总数；
- 候选发布：所选交集格之和 == 候选总数。

判定：对每个"原检出数为 1~4"的格，在上述约束下必须
- 仍存在 x_i == 0 的可行整数解，且
- 仍存在 x_i >= 5 的可行整数解。
任一不满足则该格为受影响格，发布须被拒绝，并给出该格的可行范围 [min, max]。

注意：z3 的全局默认 Context 非线程安全。每次评估使用独立 Context，
以支持多个请求并发求解（预览/发布在事件循环外的工作线程中执行）。
"""

from z3 import Context, Int, Optimize, Solver, Sum, sat

SMALL_MIN, SMALL_MAX = 1, 4   # 受保护的小样本检出数区间
SAFE_LOW, SAFE_HIGH = 0, 5    # 须同时保留 "取0" 与 "至少5" 的可行解


def _indices(cells: list[dict], selection: dict) -> list[int]:
    ds = set(selection["districts"])
    ags = set(selection["age_groups"])
    return [
        i for i, c in enumerate(cells)
        if c["district"] in ds and c["age_group"] in ags
    ]


def _feasible_range(ctx: Context, constraints, x) -> tuple[int | None, int | None]:
    opt = Optimize(ctx=ctx)
    opt.add(constraints)
    h = opt.minimize(x)
    if opt.check() != sat:
        return None, None
    lo = opt.lower(h).as_long()
    opt2 = Optimize(ctx=ctx)
    opt2.add(constraints)
    h2 = opt2.maximize(x)
    if opt2.check() != sat:
        return lo, None
    hi = opt2.upper(h2).as_long()
    return lo, hi


def evaluate(cells: list[dict], publications: list[dict], candidate: dict) -> dict:
    """评估候选发布是否安全。

    cells:        全部格 {district, age_group, population, detected}
    publications: 历次公开记录 {districts, age_groups, total}
    candidate:    候选发布 {districts, age_groups, total}
    返回 {"safe": bool, "affected": [...]}
    """
    ctx = Context()  # 每次评估独立上下文，保证并发求解安全
    xs = [Int(f"x_{i}", ctx=ctx) for i in range(len(cells))]

    constraints = []
    for i, c in enumerate(cells):
        constraints.append(xs[i] >= 0)
        constraints.append(xs[i] <= int(c["population"]))
    for pub in list(publications) + [candidate]:
        idx = _indices(cells, pub)
        constraints.append(Sum([xs[i] for i in idx]) == int(pub["total"]))

    base = Solver(ctx=ctx)
    base.add(constraints)
    if base.check() != sat:
        # 理论上不会发生（真实格值恒满足全部约束），防御性处理。
        affected = [
            {
                "district": c["district"],
                "age_group": c["age_group"],
                "detected": c["detected"],
                "min": None,
                "max": None,
                "zero_feasible": False,
                "ge5_feasible": False,
            }
            for c in cells
            if SMALL_MIN <= c["detected"] <= SMALL_MAX
        ]
        return {"safe": False, "reason": "约束不可满足", "affected": affected}

    affected = []
    for i, c in enumerate(cells):
        if not (SMALL_MIN <= c["detected"] <= SMALL_MAX):
            continue
        s = Solver(ctx=ctx)
        s.add(constraints)
        s.add(xs[i] == SAFE_LOW)
        zero_ok = s.check() == sat
        s = Solver(ctx=ctx)
        s.add(constraints)
        s.add(xs[i] >= SAFE_HIGH)
        ge5_ok = s.check() == sat
        if not (zero_ok and ge5_ok):
            lo, hi = _feasible_range(ctx, constraints, xs[i])
            affected.append(
                {
                    "district": c["district"],
                    "age_group": c["age_group"],
                    "detected": c["detected"],
                    "min": lo,
                    "max": hi,
                    "zero_feasible": zero_ok,
                    "ge5_feasible": ge5_ok,
                }
            )
    return {"safe": not affected, "affected": affected}
