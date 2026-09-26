"""业务逻辑：登记原料、保存配方（校验 + 原子写入）、展开配方、预演标签变化、
计算过敏原截断的最小停用方案、生成多配方合并备料清单（后两者及预演均为只读）。

保存配方的规则：
1. 所有引用的原料 / 子配方必须已存在，否则整体拒绝；
2. 不允许直接或间接循环引用（配方经任意链路引用回自身），否则整体拒绝；
3. 校验全部通过后，删除旧条目 + 写入新条目在同一事务中提交，
   任何失败都不会改动数据库中原有的配方。

标签预演（label-preview）套用与保存完全相同的校验，但只做展开与对比，
不写入任何数据。

过敏原截断（allergen-cut）复用展开结果求最小命中集，同样只读，
不改动任何配方或原料数据。
"""
import itertools
from collections import defaultdict
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Ingredient, Recipe, RecipeEntry, SeqCounter, utcnow
from app.schemas import RecipeIn


class ApiError(Exception):
    """业务校验失败，由全局异常处理器转成 JSON 响应。"""

    def __init__(self, status_code: int, code: str, message: str, extra: dict | None = None):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra or {}
        super().__init__(message)


# ---------- 编号 ----------

def next_code(db: Session, prefix: str, width: int) -> str:
    counter = db.get(SeqCounter, prefix)
    if counter is None:
        counter = SeqCounter(name=prefix, value=0)
        db.add(counter)
    counter.value += 1
    db.flush()
    return f"{prefix}-{counter.value:0{width}d}"


# ---------- 原料 ----------

def create_ingredient(db: Session, name: str, allergens: list[str]) -> Ingredient:
    existing = db.scalar(select(Ingredient).where(Ingredient.name == name))
    if existing is not None:
        raise ApiError(409, "DUPLICATE_NAME", f"原料名称已存在：{name}",
                       {"ingredient_id": existing.id})
    ingredient = Ingredient(id=next_code(db, "ING", 4), name=name, allergens=allergens)
    db.add(ingredient)
    db.commit()
    db.refresh(ingredient)
    return ingredient


# ---------- 配方保存 ----------

def _validate_references(db: Session, payload: RecipeIn) -> None:
    """所有引用必须存在，否则拒绝。"""
    ingredient_refs = sorted({e.ref_id for e in payload.entries if e.ref_type == "ingredient"})
    recipe_refs = sorted({e.ref_id for e in payload.entries if e.ref_type == "recipe"})

    missing: list[dict] = []
    if ingredient_refs:
        found = set(db.scalars(
            select(Ingredient.id).where(Ingredient.id.in_(ingredient_refs))))
        missing += [{"ref_type": "ingredient", "ref_id": r}
                    for r in ingredient_refs if r not in found]
    if recipe_refs:
        found = set(db.scalars(
            select(Recipe.id).where(Recipe.id.in_(recipe_refs))))
        missing += [{"ref_type": "recipe", "ref_id": r}
                    for r in recipe_refs if r not in found]

    if missing:
        raise ApiError(400, "UNKNOWN_REFERENCE",
                       "配方引用了不存在的原料或子配方", {"missing": missing})


def _find_cycle(adjacency: dict[str, list[str]], start: str) -> list[str] | None:
    """在配方引用图中寻找从 start 出发又回到 start 的路径，返回该路径或 None。"""
    visited = {start}

    def dfs(node: str, trail: list[str]) -> list[str] | None:
        for child in adjacency.get(node, []):
            if child == start:
                return trail + [child]
            if child not in visited:
                visited.add(child)
                found = dfs(child, trail + [child])
                if found:
                    return found
        return None

    return dfs(start, [start])


def _check_no_cycle(db: Session, recipe_id: str, payload: RecipeIn) -> None:
    """以「新条目替换旧条目」后的图检测是否出现回到 recipe_id 的环。"""
    adjacency: dict[str, list[str]] = defaultdict(list)
    rows = db.execute(
        select(RecipeEntry.recipe_id, RecipeEntry.ref_id)
        .where(RecipeEntry.ref_type == "recipe")
    ).all()
    for parent_id, child_id in rows:
        if parent_id != recipe_id:  # 本配方的旧组成将被整体替换，不参与
            adjacency[parent_id].append(child_id)
    adjacency[recipe_id] = [e.ref_id for e in payload.entries if e.ref_type == "recipe"]

    cycle = _find_cycle(adjacency, recipe_id)
    if cycle:
        raise ApiError(409, "CYCLE_DETECTED",
                       "配方存在直接或间接循环引用：" + " -> ".join(cycle),
                       {"cycle": cycle})


def _validate_recipe_payload(db: Session, payload: RecipeIn,
                             recipe_id: str | None) -> Recipe | None:
    """保存与标签预演共用的全部校验（只读，不写入）。

    返回被替换的现有配方；新建（recipe_id=None）时返回 None。
    """
    if recipe_id is None:
        clash = db.scalar(select(Recipe).where(Recipe.name == payload.name))
        if clash is not None:
            raise ApiError(409, "DUPLICATE_NAME", f"配方名称已存在：{payload.name}",
                           {"recipe_id": clash.id})
        recipe = None
    else:
        recipe = db.get(Recipe, recipe_id)
        if recipe is None:
            raise ApiError(404, "RECIPE_NOT_FOUND", f"配方不存在：{recipe_id}")
        clash = db.scalar(select(Recipe).where(
            Recipe.name == payload.name, Recipe.id != recipe_id))
        if clash is not None:
            raise ApiError(409, "DUPLICATE_NAME", f"配方名称已存在：{payload.name}",
                           {"recipe_id": clash.id})

    _validate_references(db, payload)
    if recipe_id is not None:
        # 新建配方尚无编号，不可能被任何现有配方引用，也就无法成环，无需检测
        _check_no_cycle(db, recipe_id, payload)
    return recipe


def save_recipe(db: Session, payload: RecipeIn, recipe_id: str | None = None) -> Recipe:
    """新建（recipe_id=None）或整体替换一个配方。失败时不改动任何已有数据。"""
    # ---- 全部校验先行，不做任何写入 ----
    recipe = _validate_recipe_payload(db, payload, recipe_id)

    # ---- 校验通过，同一事务内完成写入 ----
    if recipe is None:
        recipe = Recipe(id=next_code(db, "REC", 4),
                        name=payload.name, description=payload.description)
        db.add(recipe)
        db.flush()
    else:
        recipe.name = payload.name
        recipe.description = payload.description
        recipe.updated_at = utcnow()
        for old in list(recipe.entries):
            db.delete(old)
        db.flush()

    for position, entry in enumerate(payload.entries, start=1):
        db.add(RecipeEntry(
            id=next_code(db, "ENT", 6),
            recipe_id=recipe.id,
            position=position,
            ref_type=entry.ref_type,
            ref_id=entry.ref_id,
            quantity=entry.quantity,
            unit=entry.unit,
        ))
    db.commit()
    db.refresh(recipe)
    return recipe


# ---------- 查询 ----------

def get_recipe_or_404(db: Session, recipe_id: str) -> Recipe:
    recipe = db.get(Recipe, recipe_id)
    if recipe is None:
        raise ApiError(404, "RECIPE_NOT_FOUND", f"配方不存在：{recipe_id}")
    return recipe


def recipe_to_out(db: Session, recipe: Recipe) -> dict:
    """配方详情：条目按行号排序并解析引用名称。"""
    names = _load_names(db)
    return {
        "id": recipe.id,
        "name": recipe.name,
        "description": recipe.description,
        "created_at": recipe.created_at,
        "updated_at": recipe.updated_at,
        "entries": [
            {
                "id": e.id,
                "position": e.position,
                "ref_type": e.ref_type,
                "ref_id": e.ref_id,
                "ref_name": names.get((e.ref_type, e.ref_id), e.ref_id),
                "quantity": e.quantity,
                "unit": e.unit,
            }
            for e in sorted(recipe.entries, key=lambda x: x.position)
        ],
    }


def _load_names(db: Session) -> dict[tuple[str, str], str]:
    names: dict[tuple[str, str], str] = {}
    for row in db.scalars(select(Ingredient)):
        names[("ingredient", row.id)] = row.name
    for row in db.scalars(select(Recipe)):
        names[("recipe", row.id)] = row.name
    return names


# ---------- 配方展开 ----------

def _load_graph(db: Session):
    """加载全部配方、原料与条目（条目转为 dict 并按行号排序），供展开使用。"""
    recipes = {r.id: r for r in db.scalars(select(Recipe))}
    ingredients = {i.id: i for i in db.scalars(select(Ingredient))}
    entries_by_recipe: dict[str, list[dict]] = defaultdict(list)
    for e in db.scalars(select(RecipeEntry)):
        entries_by_recipe[e.recipe_id].append({
            "entry_id": e.id,
            "position": e.position,
            "recipe_id": e.recipe_id,
            "ref_type": e.ref_type,
            "ref_id": e.ref_id,
            "quantity": e.quantity,
            "unit": e.unit,
        })
    for entries in entries_by_recipe.values():
        entries.sort(key=lambda x: x["position"])
    return recipes, ingredients, entries_by_recipe


def _expand_paths(root_id: str, root_entries: list[dict],
                  entries_by_recipe: dict[str, list[dict]],
                  recipes: dict, ingredients: dict) -> list[dict]:
    """从根配方的条目出发，展开全部直达原料叶子的路径。

    同一子配方被使用多次时，每次使用都有各自的条目（位置），
    因此会产生多条相互独立的路径，全部保留。
    """
    leaves: list[dict] = []

    def walk(entries: list[dict], prefix: list[dict], ancestors: frozenset[str]) -> None:
        for entry in entries:
            parent = recipes[entry["recipe_id"]]
            if entry["ref_type"] == "ingredient":
                ref_name = ingredients[entry["ref_id"]].name
            else:
                ref_name = recipes[entry["ref_id"]].name
            step = {
                "entry_id": entry["entry_id"],
                "position": entry["position"],
                "recipe_id": parent.id,
                "recipe_name": parent.name,
                "ref_type": entry["ref_type"],
                "ref_id": entry["ref_id"],
                "ref_name": ref_name,
                "quantity": entry["quantity"],
                "unit": entry["unit"],
            }
            trail = prefix + [step]
            if entry["ref_type"] == "ingredient":
                ingredient = ingredients[entry["ref_id"]]
                leaves.append({
                    "steps": trail,
                    "ingredient_id": ingredient.id,
                    "ingredient_name": ingredient.name,
                    "allergens": list(ingredient.allergens),
                })
            else:
                # 保存时已保证无环；此处仍防御性截断，避免脏数据导致死循环
                if entry["ref_id"] in ancestors:
                    continue
                walk(entries_by_recipe.get(entry["ref_id"], []),
                     trail, ancestors | {entry["ref_id"]})

    walk(root_entries, [], frozenset({root_id}))
    return leaves


def expand_recipe(db: Session, recipe_id: str) -> dict:
    """展开配方的全部引用路径，并按过敏原汇总每条来源路径。"""
    root = get_recipe_or_404(db, recipe_id)
    recipes, ingredients, entries_by_recipe = _load_graph(db)
    leaves = _expand_paths(root.id, entries_by_recipe.get(root.id, []),
                           entries_by_recipe, recipes, ingredients)

    # 对外路径标识使用条目编号（ENT-xxxxxx）
    paths = [
        {
            "path": [s["entry_id"] for s in leaf["steps"]],
            "steps": leaf["steps"],
            "ingredient_id": leaf["ingredient_id"],
            "ingredient_name": leaf["ingredient_name"],
            "allergens": leaf["allergens"],
        }
        for leaf in leaves
    ]

    # 按过敏原汇总：每个过敏原 -> 带来源路径的列表
    summary: dict[str, list[dict]] = defaultdict(list)
    for leaf in paths:
        for allergen in leaf["allergens"]:
            summary[allergen].append({
                "path": leaf["path"],
                "ingredient_id": leaf["ingredient_id"],
                "ingredient_name": leaf["ingredient_name"],
            })

    return {
        "recipe_id": root.id,
        "recipe_name": root.name,
        "leaf_count": len(paths),
        "paths": paths,
        "allergen_summary": [
            {"allergen": allergen, "sources": summary[allergen]}
            for allergen in sorted(summary)
        ],
    }


# ---------- 标签变化预演 ----------

def _allergen_sources(leaves: list[dict]) -> dict[str, dict]:
    """过敏原 -> {(原料编号, 位置路径): 原料名称}。

    路径标识由每层条目位置组成；重复引用因所在条目位置不同，
    天然成为不同来源，全部保留。
    """
    sources: dict[str, dict] = defaultdict(dict)
    for leaf in leaves:
        path = tuple(step["position"] for step in leaf["steps"])
        for allergen in leaf["allergens"]:
            sources[allergen][(leaf["ingredient_id"], path)] = leaf["ingredient_name"]
    return sources


def _source_list(sources: dict) -> list[dict]:
    """来源按路径排序输出，便于人工逐项核对。"""
    return [
        {"ingredient_id": ingredient_id, "ingredient_name": name, "path": list(path)}
        for (ingredient_id, path), name in sorted(
            sources.items(), key=lambda item: (item[0][1], item[0][0]))
    ]


def preview_label_change(db: Session, recipe_id: str, payload: RecipeIn) -> dict:
    """预演「整体替换」后的过敏原标签变化，不写入任何数据。

    校验规则与保存接口完全一致；分别展开当前配方与预演配方，
    按过敏原对比来源（原料 + 每层条目位置组成的路径），
    给出新增、移除、以及来源原料或路径发生变化的过敏原。
    """
    recipe = get_recipe_or_404(db, recipe_id)
    _validate_recipe_payload(db, payload, recipe_id)  # 与保存相同的校验，失败即报同样的错

    recipes, ingredients, entries_by_recipe = _load_graph(db)

    # 当前配方：条目与位置均取自数据库
    current = _expand_paths(recipe_id, entries_by_recipe.get(recipe_id, []),
                            entries_by_recipe, recipes, ingredients)

    # 预演配方：根条目来自请求体（位置从 1 起，与保存时一致），子配方仍取数据库
    preview_entries = [
        {
            "entry_id": None,  # 未保存，尚无条目编号
            "position": position,
            "recipe_id": recipe_id,
            "ref_type": e.ref_type,
            "ref_id": e.ref_id,
            "quantity": e.quantity,
            "unit": e.unit,
        }
        for position, e in enumerate(payload.entries, start=1)
    ]
    preview_recipes = dict(recipes)
    preview_recipes[recipe_id] = SimpleNamespace(id=recipe_id, name=payload.name)
    preview = _expand_paths(recipe_id, preview_entries, entries_by_recipe,
                            preview_recipes, ingredients)

    before = _allergen_sources(current)
    after = _allergen_sources(preview)

    added, removed, changed = [], [], []
    for allergen in sorted(set(before) | set(after)):
        b, a = before.get(allergen, {}), after.get(allergen, {})
        item = {"allergen": allergen,
                "before": _source_list(b), "after": _source_list(a)}
        if not b:
            added.append(item)
        elif not a:
            removed.append(item)
        elif set(b) != set(a):  # 仍存在，但来源原料或路径变了
            changed.append(item)

    return {
        "recipe_id": recipe_id,
        "recipe_name": recipe.name,
        "preview_name": payload.name,
        "added_allergens": added,
        "removed_allergens": removed,
        "changed_allergens": changed,
    }


# ---------- 过敏原截断方案（最小命中集，只读） ----------

# 全局最小命中集的求解规模上限：超出即拒绝（422），避免指数级求解拖垮服务
CUT_MAX_PATHS = 64       # 含目标过敏原、需要截断的原料路径总数上限
CUT_MAX_CANDIDATES = 24  # 这些路径上出现的候选条目数上限


def _min_hitting_set(paths: list[list[str]], candidates: list[str]) -> list[str]:
    """求全局最小命中集；同样数量时按升序条目编号列表的字典序取最小。

    candidates 必须已按升序排列。实现要点：
    1. 只含一个候选条目的路径，其条目在任何解中都不可少，先行收编为必选项；
       必选项存在于所有解中，不影响剩余部分的字典序比较；
    2. 其余按组合大小从小到大枚举（下界由互不相交路径数给出），
       同大小内按编号升序的字典序枚举，第一个命中全部剩余路径的组合即答案。
    """
    bit = {entry_id: 1 << i for i, entry_id in enumerate(candidates)}

    # 路径 -> 候选条目位掩码（相同掩码去重，命中其一即命中全部）
    masks: list[int] = []
    seen: set[int] = set()
    for path in paths:
        mask = 0
        for entry_id in path:
            mask |= bit[entry_id]
        if mask not in seen:
            seen.add(mask)
            masks.append(mask)

    # 1. 必选项：单条目路径要求的条目
    forced: set[str] = set()
    for mask in masks:
        if mask and not mask & (mask - 1):  # 该路径只有一个候选条目可截断
            forced.add(candidates[mask.bit_length() - 1])
    forced_mask = 0
    for entry_id in forced:
        forced_mask |= bit[entry_id]

    rest_masks = [m for m in masks if not m & forced_mask]
    rest_candidates = [c for c in candidates if c not in forced]

    # 2. 下界：互不相交的路径各需一个不同条目，用于跳过不可能的组合大小
    lower = 0
    used = 0
    for mask in sorted(rest_masks, key=lambda m: m.bit_count()):
        if not mask & used:
            lower += 1
            used |= mask

    for size in range(lower, len(rest_candidates) + 1):
        for combo in itertools.combinations(rest_candidates, size):
            combo_mask = 0
            for entry_id in combo:
                combo_mask |= bit[entry_id]
            if all(m & combo_mask for m in rest_masks):
                return sorted(forced | set(combo))
    return sorted(forced)  # 候选全集必然命中所有路径，不会走到这里


def plan_allergen_cut(db: Session, recipe_id: str, allergen: str) -> dict:
    """最小停用方案：找出最少需要停用的条目，截断全部含目标过敏原的原料路径。

    只读操作：复用配方展开结果取完整条目路径，不改动任何配方或原料数据。
    条目编号全局唯一：同一子配方被多处引用时，其条目出现在每条引用路径中，
    停用一次即覆盖所有引用实例——这正是按条目编号求命中集的含义。
    """
    expansion = expand_recipe(db, recipe_id)  # 配方不存在时抛 404

    # 含目标过敏原的原料路径 = 需要截断的路径
    targets = [p for p in expansion["paths"] if allergen in p["allergens"]]
    plan = {
        "recipe_id": expansion["recipe_id"],
        "recipe_name": expansion["recipe_name"],
        "allergen": allergen,
        "entry_ids": [],
        "entries": [],
        "cut_path_count": 0,
    }
    if not targets:  # 空路径集合 -> 空方案
        return plan

    candidates = sorted({entry_id for p in targets for entry_id in p["path"]})
    if len(targets) > CUT_MAX_PATHS or len(candidates) > CUT_MAX_CANDIDATES:
        raise ApiError(
            422, "CUT_PROBLEM_TOO_LARGE",
            "命中集求解规模超出上限（路径总数 ≤ "
            f"{CUT_MAX_PATHS}、候选条目数 ≤ {CUT_MAX_CANDIDATES}），"
            "请缩小配方规模或拆分目标",
            {
                "path_count": len(targets),
                "candidate_count": len(candidates),
                "max_paths": CUT_MAX_PATHS,
                "max_candidates": CUT_MAX_CANDIDATES,
            },
        )

    chosen = _min_hitting_set([p["path"] for p in targets], candidates)

    entries = []
    cut_indices: set[int] = set()
    for entry_id in chosen:
        covered = []
        for i, p in enumerate(targets):
            if entry_id in p["path"]:
                cut_indices.add(i)
                covered.append({
                    "path": p["path"],
                    "ingredient_id": p["ingredient_id"],
                    "ingredient_name": p["ingredient_name"],
                })
        entries.append({"entry_id": entry_id, "covered_paths": covered})

    plan["entry_ids"] = chosen
    plan["entries"] = entries
    plan["cut_path_count"] = len(cut_indices)
    return plan


# ---------- 多配方合并备料清单（只读） ----------

def _decimal_text(value) -> str:
    """Decimal 转字符串：去掉多余尾零（整数不带小数点），但绝不使用科学计数法。"""
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def build_material_plan(db: Session, items: list) -> dict:
    """为多个配方的各自正数批次数生成合并备料清单（只读，不写入任何数据）。

    沿现有配方引用逐层展开：子配方条目的 quantity 表示每批使用的子配方批数，
    路径上的批数逐层相乘；同一子配方被重复引用时，每条引用分别展开、分别计入。
    原料用量按（原料编号, unit）分组，以十进制累加；不同单位不做换算。

    任一可达条目缺少 quantity、原料条目缺少 unit，或子配方条目的 unit 不是「份」，
    则收集全部问题条目编号后一次性返回 422，不返回部分清单。
    """
    # 1. 请求的配方必须全部存在，否则 404
    roots: list[tuple[str, Decimal]] = []
    for item in items:
        recipe = db.get(Recipe, item.recipe_id)
        if recipe is None:
            raise ApiError(404, "RECIPE_NOT_FOUND", f"配方不存在：{item.recipe_id}",
                           {"recipe_id": item.recipe_id})
        roots.append((recipe.id, Decimal(str(item.batches))))

    recipes, ingredients, entries_by_recipe = _load_graph(db)

    # 2. 深度优先展开，沿途收集全部问题条目；multiplier 为 None 表示路径已失效
    problems: list[str] = []
    totals: dict[tuple[str, str], Decimal] = defaultdict(lambda: Decimal("0"))

    def walk(recipe_id: str, multiplier, ancestors: frozenset[str]) -> None:
        for entry in entries_by_recipe.get(recipe_id, []):
            entry_id = entry["entry_id"]
            qty_ok = entry["quantity"] is not None
            unit_ok = entry["unit"] is not None and entry["unit"] != ""
            if not qty_ok:
                problems.append(entry_id)

            if entry["ref_type"] == "ingredient":
                if not unit_ok:  # 原料条目缺少 unit
                    problems.append(entry_id)
                elif multiplier is not None and qty_ok:
                    totals[(entry["ref_id"], entry["unit"])] += (
                        multiplier * Decimal(str(entry["quantity"])))
                continue

            # 子配方条目：unit 必须是「份」
            if entry["unit"] != "份":
                problems.append(entry_id)
            # 保存时已保证无环且引用存在；此处防御性截断，避免脏数据死循环
            if entry["ref_id"] in ancestors:
                continue
            next_multiplier = (
                multiplier * Decimal(str(entry["quantity"]))
                if multiplier is not None and qty_ok else None)
            walk(entry["ref_id"], next_multiplier, ancestors | {entry["ref_id"]})

    for recipe_id, batches in roots:
        walk(recipe_id, batches, frozenset({recipe_id}))

    # 3. 任何问题都整体拒绝，不返回部分清单
    if problems:
        entry_ids = sorted(set(problems))
        raise ApiError(
            422, "MATERIAL_PLAN_INVALID_ENTRIES",
            "存在无法参与备料计算的条目：条目缺少 quantity、原料条目缺少 unit，"
            "或子配方条目的 unit 不是「份」",
            {"entry_ids": entry_ids, "entry_count": len(entry_ids)},
        )

    # 4. 按原料编号、单位排序，十进制累加结果以字符串返回
    plan_items = []
    for ingredient_id, unit in sorted(totals):
        ingredient = ingredients.get(ingredient_id)
        plan_items.append({
            "ingredient_id": ingredient_id,
            "ingredient_name": ingredient.name if ingredient is not None else ingredient_id,
            "unit": unit,
            "quantity": _decimal_text(totals[(ingredient_id, unit)]),
        })
    return {"items": plan_items, "item_count": len(plan_items)}
