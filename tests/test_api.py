"""接口行为测试：登记、校验、循环拒绝、原子保存、展开汇总。"""


def make_ingredient(client, name, allergens=None):
    resp = client.post("/ingredients", json={"name": name, "allergens": allergens or []})
    assert resp.status_code == 201, resp.text
    return resp.json()


def make_recipe(client, name, entries, description=""):
    resp = client.post("/recipes", json={
        "name": name, "description": description, "entries": entries,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------- 原料 ----------

def test_create_and_list_ingredients(client):
    ing = make_ingredient(client, "黄油", ["乳", "乳"])  # 重复过敏原应去重
    assert ing["id"] == "ING-0001"
    assert ing["allergens"] == ["乳"]

    make_ingredient(client, "低筋面粉", ["麸质"])
    listed = client.get("/ingredients").json()
    assert [i["id"] for i in listed] == ["ING-0001", "ING-0002"]


def test_duplicate_ingredient_name_rejected(client):
    make_ingredient(client, "黄油")
    resp = client.post("/ingredients", json={"name": "黄油", "allergens": []})
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "DUPLICATE_NAME"


# ---------- 配方保存校验 ----------

def test_unknown_reference_rejected_and_nothing_saved(client):
    resp = client.post("/recipes", json={
        "name": "幽灵配方",
        "entries": [
            {"ref_type": "ingredient", "ref_id": "ING-9999"},
            {"ref_type": "recipe", "ref_id": "REC-9999"},
        ],
    })
    assert resp.status_code == 400
    body = resp.json()["detail"]
    assert body["code"] == "UNKNOWN_REFERENCE"
    assert len(body["missing"]) == 2
    # 失败不应产生任何配方
    assert client.get("/recipes").json() == []


def test_direct_self_cycle_rejected(client):
    butter = make_ingredient(client, "黄油", ["乳"])
    sub = make_recipe(client, "基底", [
        {"ref_type": "ingredient", "ref_id": butter["id"]},
    ])
    resp = client.put(f"/recipes/{sub['id']}", json={
        "name": "基底",
        "entries": [{"ref_type": "recipe", "ref_id": sub["id"]}],  # 直接引用自己
    })
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "CYCLE_DETECTED"


def test_indirect_cycle_rejected_and_original_kept(client):
    milk = make_ingredient(client, "牛奶", ["乳"])
    a = make_recipe(client, "配方A", [{"ref_type": "ingredient", "ref_id": milk["id"]}])
    b = make_recipe(client, "配方B", [{"ref_type": "recipe", "ref_id": a["id"]}])
    # 让 A 引用 B，形成 A -> B -> A 的间接循环
    resp = client.put(f"/recipes/{a['id']}", json={
        "name": "配方A",
        "entries": [{"ref_type": "recipe", "ref_id": b["id"]}],
    })
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "CYCLE_DETECTED"

    # 失败时原配方保持不变
    after = client.get(f"/recipes/{a['id']}").json()
    assert len(after["entries"]) == 1
    assert after["entries"][0]["ref_type"] == "ingredient"
    assert after["entries"][0]["ref_id"] == milk["id"]


def test_failed_update_with_unknown_ref_keeps_original(client):
    flour = make_ingredient(client, "面粉", ["麸质"])
    recipe = make_recipe(client, "面团", [
        {"ref_type": "ingredient", "ref_id": flour["id"], "quantity": 500, "unit": "g"},
    ])
    resp = client.put(f"/recipes/{recipe['id']}", json={
        "name": "面团",
        "entries": [{"ref_type": "ingredient", "ref_id": "ING-4242"}],
    })
    assert resp.status_code == 400
    after = client.get(f"/recipes/{recipe['id']}").json()
    assert after["entries"][0]["ref_id"] == flour["id"]
    assert after["entries"][0]["quantity"] == 500


def test_update_replaces_entries_atomically(client):
    flour = make_ingredient(client, "面粉", ["麸质"])
    sugar = make_ingredient(client, "砂糖")
    recipe = make_recipe(client, "面团", [
        {"ref_type": "ingredient", "ref_id": flour["id"]},
    ])
    old_entry_id = recipe["entries"][0]["id"]

    resp = client.put(f"/recipes/{recipe['id']}", json={
        "name": "甜面团",
        "entries": [
            {"ref_type": "ingredient", "ref_id": flour["id"]},
            {"ref_type": "ingredient", "ref_id": sugar["id"]},
        ],
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "甜面团"
    assert len(body["entries"]) == 2
    # 新条目使用新的独立条目编号
    assert all(e["id"] != old_entry_id for e in body["entries"])


# ---------- 展开与过敏原汇总 ----------

def _build_sample(client):
    """构造：曲奇 = 面粉 + 2 次黄油霜；黄油霜 = 黄油 + 糖。"""
    flour = make_ingredient(client, "面粉", ["麸质"])
    butter = make_ingredient(client, "黄油", ["乳"])
    sugar = make_ingredient(client, "砂糖")
    cream = make_recipe(client, "黄油霜", [
        {"ref_type": "ingredient", "ref_id": butter["id"], "quantity": 250, "unit": "g"},
        {"ref_type": "ingredient", "ref_id": sugar["id"], "quantity": 100, "unit": "g"},
    ])
    cookie = make_recipe(client, "曲奇", [
        {"ref_type": "ingredient", "ref_id": flour["id"], "quantity": 300, "unit": "g"},
        {"ref_type": "recipe", "ref_id": cream["id"], "quantity": 1, "unit": "份"},
        {"ref_type": "recipe", "ref_id": cream["id"], "quantity": 1, "unit": "份"},
    ])
    return flour, butter, sugar, cream, cookie


def test_expansion_expands_all_paths(client):
    _, butter, _, cream, cookie = _build_sample(client)
    data = client.get(f"/recipes/{cookie['id']}/expansion").json()

    assert data["recipe_id"] == cookie["id"]
    # 面粉 1 条 + 黄油霜×2 各展开 2 条 = 5 条叶子路径
    assert data["leaf_count"] == 5

    butter_paths = [p for p in data["paths"] if p["ingredient_id"] == butter["id"]]
    assert len(butter_paths) == 2  # 同一子配方用了两次，分别保留
    for leaf in butter_paths:
        assert len(leaf["path"]) == 2                       # 曲奇条目 -> 黄油霜条目
        assert leaf["steps"][0]["ref_id"] == cream["id"]    # 第一跳是子配方
        assert leaf["steps"][1]["ref_id"] == butter["id"]   # 第二跳是原料


def test_reused_sub_recipe_keeps_separate_entry_ids(client):
    _, butter, _, _, cookie = _build_sample(client)
    data = client.get(f"/recipes/{cookie['id']}/expansion").json()

    butter_paths = [p["path"] for p in data["paths"] if p["ingredient_id"] == butter["id"]]
    first_hops = {p[0] for p in butter_paths}
    assert len(first_hops) == 2  # 两次使用各有独立条目编号

    # 过敏原汇总中，每条来源路径也分别列出
    summary = {item["allergen"]: item["sources"] for item in data["allergen_summary"]}
    milk_sources = summary["乳"]
    assert len(milk_sources) == 2
    assert {tuple(s["path"]) for s in milk_sources} == {tuple(p) for p in butter_paths}


def test_allergen_summary_groups_by_allergen(client):
    flour, butter, _, _, cookie = _build_sample(client)
    data = client.get(f"/recipes/{cookie['id']}/expansion").json()
    summary = {item["allergen"]: item["sources"] for item in data["allergen_summary"]}

    assert set(summary) == {"乳", "麸质"}
    assert [s["ingredient_id"] for s in summary["麸质"]] == [flour["id"]]
    assert all(s["ingredient_id"] == butter["id"] for s in summary["乳"])


def test_expansion_of_unknown_recipe_404(client):
    resp = client.get("/recipes/REC-9999/expansion")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "RECIPE_NOT_FOUND"


def test_deep_nesting_expands_full_chain(client):
    nut = make_ingredient(client, "杏仁", ["坚果"])
    inner = make_recipe(client, "内馅", [{"ref_type": "ingredient", "ref_id": nut["id"]}])
    mid = make_recipe(client, "中层", [{"ref_type": "recipe", "ref_id": inner["id"]}])
    outer = make_recipe(client, "成品", [{"ref_type": "recipe", "ref_id": mid["id"]}])

    data = client.get(f"/recipes/{outer['id']}/expansion").json()
    assert data["leaf_count"] == 1
    leaf = data["paths"][0]
    assert len(leaf["path"]) == 3  # 三层条目编号依次记录
    assert leaf["allergens"] == ["坚果"]
    assert data["allergen_summary"][0]["allergen"] == "坚果"


# ---------- 标签变化预演 ----------

def test_label_preview_added_removed_changed(client):
    flour = make_ingredient(client, "面粉", ["麸质"])
    butter = make_ingredient(client, "黄油", ["乳"])
    margarine = make_ingredient(client, "人造黄油", ["乳"])
    peanut = make_ingredient(client, "花生酱", ["花生"])
    recipe = make_recipe(client, "曲奇", [
        {"ref_type": "ingredient", "ref_id": flour["id"]},
        {"ref_type": "ingredient", "ref_id": butter["id"]},
    ])

    resp = client.post(f"/recipes/{recipe['id']}/label-preview", json={
        "name": "曲奇",
        "entries": [
            {"ref_type": "ingredient", "ref_id": margarine["id"]},  # 乳：来源原料变化
            {"ref_type": "ingredient", "ref_id": peanut["id"]},     # 花生：新增
        ],
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["recipe_id"] == recipe["id"]
    assert data["recipe_name"] == "曲奇"
    assert data["preview_name"] == "曲奇"

    assert [i["allergen"] for i in data["added_allergens"]] == ["花生"]
    added = data["added_allergens"][0]
    assert added["before"] == []
    assert added["after"] == [
        {"ingredient_id": peanut["id"], "ingredient_name": "花生酱", "path": [2]}]

    assert [i["allergen"] for i in data["removed_allergens"]] == ["麸质"]
    removed = data["removed_allergens"][0]
    assert removed["before"] == [
        {"ingredient_id": flour["id"], "ingredient_name": "面粉", "path": [1]}]
    assert removed["after"] == []

    changed = {i["allergen"]: i for i in data["changed_allergens"]}
    assert set(changed) == {"乳"}
    assert changed["乳"]["before"] == [
        {"ingredient_id": butter["id"], "ingredient_name": "黄油", "path": [2]}]
    assert changed["乳"]["after"] == [
        {"ingredient_id": margarine["id"], "ingredient_name": "人造黄油", "path": [1]}]


def test_label_preview_detects_path_change(client):
    butter = make_ingredient(client, "黄油", ["乳"])
    sugar = make_ingredient(client, "砂糖")
    recipe = make_recipe(client, "霜", [
        {"ref_type": "ingredient", "ref_id": butter["id"]},  # 位置 1
        {"ref_type": "ingredient", "ref_id": sugar["id"]},
    ])

    resp = client.post(f"/recipes/{recipe['id']}/label-preview", json={
        "name": "霜",
        "entries": [
            {"ref_type": "ingredient", "ref_id": sugar["id"]},
            {"ref_type": "ingredient", "ref_id": butter["id"]},  # 移到位置 2
        ],
    })
    data = resp.json()
    assert data["added_allergens"] == []
    assert data["removed_allergens"] == []
    # 过敏原仍在，但来源路径从 [1] 变为 [2]
    changed = data["changed_allergens"]
    assert [c["allergen"] for c in changed] == ["乳"]
    assert changed[0]["before"][0]["path"] == [1]
    assert changed[0]["after"][0]["path"] == [2]


def test_label_preview_keeps_duplicate_sources_distinct(client):
    butter = make_ingredient(client, "黄油", ["乳"])
    cream = make_recipe(client, "黄油霜", [
        {"ref_type": "ingredient", "ref_id": butter["id"]},
    ])
    cookie = make_recipe(client, "曲奇", [
        {"ref_type": "recipe", "ref_id": cream["id"]},
    ])

    resp = client.post(f"/recipes/{cookie['id']}/label-preview", json={
        "name": "曲奇",
        "entries": [
            {"ref_type": "recipe", "ref_id": cream["id"]},
            {"ref_type": "recipe", "ref_id": cream["id"]},  # 重复引用同一子配方
        ],
    })
    data = resp.json()
    changed = {c["allergen"]: c for c in data["changed_allergens"]}
    milk = changed["乳"]
    assert milk["before"] == [
        {"ingredient_id": butter["id"], "ingredient_name": "黄油", "path": [1, 1]}]
    # 两次引用以各自的位置路径保留为不同来源
    assert [s["path"] for s in milk["after"]] == [[1, 1], [2, 1]]
    assert all(s["ingredient_id"] == butter["id"] for s in milk["after"])


def test_label_preview_no_change_gives_empty_groups(client):
    butter = make_ingredient(client, "黄油", ["乳"])
    recipe = make_recipe(client, "霜", [
        {"ref_type": "ingredient", "ref_id": butter["id"]},
    ])
    resp = client.post(f"/recipes/{recipe['id']}/label-preview", json={
        "name": "霜",
        "entries": [{"ref_type": "ingredient", "ref_id": butter["id"]}],
    })
    data = resp.json()
    assert data["added_allergens"] == []
    assert data["removed_allergens"] == []
    assert data["changed_allergens"] == []


def test_label_preview_does_not_write(client):
    butter = make_ingredient(client, "黄油", ["乳"])
    peanut = make_ingredient(client, "花生酱", ["花生"])
    recipe = make_recipe(client, "霜", [
        {"ref_type": "ingredient", "ref_id": butter["id"]},
    ])
    before = client.get(f"/recipes/{recipe['id']}").json()

    resp = client.post(f"/recipes/{recipe['id']}/label-preview", json={
        "name": "新霜",
        "entries": [{"ref_type": "ingredient", "ref_id": peanut["id"]}],
    })
    assert resp.status_code == 200, resp.text

    # 预演后配方名称、条目（含条目编号）完全不变
    after = client.get(f"/recipes/{recipe['id']}").json()
    assert after == before


def test_label_preview_validation_matches_save(client):
    butter = make_ingredient(client, "黄油", ["乳"])
    make_recipe(client, "别的", [])
    recipe = make_recipe(client, "霜", [
        {"ref_type": "ingredient", "ref_id": butter["id"]},
    ])

    # 引用不存在 -> 与保存接口相同的 400
    resp = client.post(f"/recipes/{recipe['id']}/label-preview", json={
        "name": "霜",
        "entries": [{"ref_type": "ingredient", "ref_id": "ING-9999"}],
    })
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "UNKNOWN_REFERENCE"

    # 名称冲突 -> 与保存接口相同的 409
    resp = client.post(f"/recipes/{recipe['id']}/label-preview", json={
        "name": "别的", "entries": [],
    })
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "DUPLICATE_NAME"

    # 循环引用 -> 与保存接口相同的 409
    resp = client.post(f"/recipes/{recipe['id']}/label-preview", json={
        "name": "霜",
        "entries": [{"ref_type": "recipe", "ref_id": recipe["id"]}],
    })
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "CYCLE_DETECTED"

    # 配方不存在 -> 404
    resp = client.post("/recipes/REC-9999/label-preview",
                       json={"name": "x", "entries": []})
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "RECIPE_NOT_FOUND"

    # 全部失败后，原配方保持不变
    after = client.get(f"/recipes/{recipe['id']}").json()
    assert after["name"] == "霜"
    assert [e["ref_id"] for e in after["entries"]] == [butter["id"]]


# ---------- 过敏原截断方案（最小命中集） ----------

def test_allergen_cut_shared_sub_recipe_entry_covers_all_instances(client):
    """共用子配方中的条目只停用一次，即覆盖其所有引用实例。"""
    flour, butter, _, cream, cookie = _build_sample(client)
    butter_entry = cream["entries"][0]["id"]  # 黄油霜中的黄油条目

    before = client.get(f"/recipes/{cookie['id']}").json()
    resp = client.post(f"/recipes/{cookie['id']}/allergen-cut",
                       json={"allergen": "乳"})
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["recipe_id"] == cookie["id"]
    assert data["allergen"] == "乳"
    # 停用子配方中的黄油条目一次，两条引用路径都被截断
    assert data["entry_ids"] == [butter_entry]
    assert data["cut_path_count"] == 2
    covered = data["entries"][0]["covered_paths"]
    assert len(covered) == 2
    assert all(p["ingredient_id"] == butter["id"] for p in covered)
    assert all(p["path"][-1] == butter_entry for p in covered)
    assert len({p["path"][0] for p in covered}) == 2  # 两次引用各占一条路径

    # 只读：配方数据不变
    assert client.get(f"/recipes/{cookie['id']}").json() == before


def test_allergen_cut_prefers_root_entries_when_fewer(client):
    """子配方被引用两次且含 3 个过敏原原料：停用 2 个根条目优于 3 个子条目。"""
    nut = make_ingredient(client, "杏仁", ["坚果"])
    mix = make_recipe(client, "坚果碎", [
        {"ref_type": "ingredient", "ref_id": nut["id"]},
        {"ref_type": "ingredient", "ref_id": nut["id"]},
        {"ref_type": "ingredient", "ref_id": nut["id"]},
    ])
    cake = make_recipe(client, "蛋糕", [
        {"ref_type": "recipe", "ref_id": mix["id"]},
        {"ref_type": "recipe", "ref_id": mix["id"]},
    ])

    data = client.post(f"/recipes/{cake['id']}/allergen-cut",
                       json={"allergen": "坚果"}).json()
    root_entry_ids = [e["id"] for e in cake["entries"]]
    assert data["entry_ids"] == sorted(root_entry_ids)  # 2 < 3，选根条目
    assert data["cut_path_count"] == 6


def test_allergen_cut_tie_breaks_by_lexicographic_entry_ids(client):
    """同样数量时，按升序条目编号列表的字典序取最小。"""
    nut = make_ingredient(client, "杏仁", ["坚果"])
    peanut = make_ingredient(client, "花生", ["坚果"])
    # 子配方含 2 个过敏原条目（ENT-000001/2），主配方引用两次（ENT-000003/4）
    mix = make_recipe(client, "坚果碎", [
        {"ref_type": "ingredient", "ref_id": nut["id"]},
        {"ref_type": "ingredient", "ref_id": peanut["id"]},
    ])
    cake = make_recipe(client, "蛋糕", [
        {"ref_type": "recipe", "ref_id": mix["id"]},
        {"ref_type": "recipe", "ref_id": mix["id"]},
    ])

    data = client.post(f"/recipes/{cake['id']}/allergen-cut",
                       json={"allergen": "坚果"}).json()
    # 两种最小方案：{子配方两条目} 或 {主配方两条目}，前者编号更小
    sub_entry_ids = sorted(e["id"] for e in mix["entries"])
    root_entry_ids = sorted(e["id"] for e in cake["entries"])
    assert sub_entry_ids < root_entry_ids
    assert data["entry_ids"] == sub_entry_ids
    assert data["cut_path_count"] == 4


def test_allergen_cut_empty_when_allergen_absent(client):
    """空路径集合返回空方案。"""
    _build_sample(client)  # 只含 乳、麸质
    recipes = client.get("/recipes").json()
    cookie_id = recipes[1]["id"]

    data = client.post(f"/recipes/{cookie_id}/allergen-cut",
                       json={"allergen": "花生"}).json()
    assert data["entry_ids"] == []
    assert data["entries"] == []
    assert data["cut_path_count"] == 0


def test_allergen_cut_empty_recipe_gives_empty_plan(client):
    recipe = make_recipe(client, "空配方", [])
    data = client.post(f"/recipes/{recipe['id']}/allergen-cut",
                       json={"allergen": "乳"}).json()
    assert data["entry_ids"] == []
    assert data["entries"] == []
    assert data["cut_path_count"] == 0


def test_allergen_cut_unknown_recipe_404(client):
    resp = client.post("/recipes/REC-9999/allergen-cut", json={"allergen": "乳"})
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "RECIPE_NOT_FOUND"


def test_allergen_cut_blank_allergen_rejected(client):
    recipe = make_recipe(client, "空配方", [])
    resp = client.post(f"/recipes/{recipe['id']}/allergen-cut",
                       json={"allergen": "   "})
    assert resp.status_code == 422


def test_allergen_cut_too_many_candidates_422(client):
    """候选条目数超过 24 -> 422 并说明规模。"""
    milk = make_ingredient(client, "牛奶", ["乳"])
    recipe = make_recipe(client, "大配方", [
        {"ref_type": "ingredient", "ref_id": milk["id"]} for _ in range(25)
    ])

    resp = client.post(f"/recipes/{recipe['id']}/allergen-cut",
                       json={"allergen": "乳"})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "CUT_PROBLEM_TOO_LARGE"
    assert detail["path_count"] == 25
    assert detail["candidate_count"] == 25
    assert detail["max_paths"] == 64
    assert detail["max_candidates"] == 24


def test_allergen_cut_too_many_paths_422(client):
    """路径总数超过 64 -> 422（候选条目数未超也一样拒绝）。"""
    milk = make_ingredient(client, "牛奶", ["乳"])
    sub = make_recipe(client, "奶基底", [
        {"ref_type": "ingredient", "ref_id": milk["id"]} for _ in range(9)
    ])
    recipe = make_recipe(client, "大配方", [
        {"ref_type": "recipe", "ref_id": sub["id"]} for _ in range(8)
    ])  # 9 × 8 = 72 条路径，候选条目 9 + 8 = 17

    resp = client.post(f"/recipes/{recipe['id']}/allergen-cut",
                       json={"allergen": "乳"})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "CUT_PROBLEM_TOO_LARGE"
    assert detail["path_count"] == 72
    assert detail["candidate_count"] == 17


def test_allergen_cut_at_limits_still_solved(client):
    """64 条路径、16 个候选条目（上限内）仍可求出全局最小解。"""
    milk = make_ingredient(client, "牛奶", ["乳"])
    sub = make_recipe(client, "奶基底", [
        {"ref_type": "ingredient", "ref_id": milk["id"]} for _ in range(8)
    ])
    recipe = make_recipe(client, "大配方", [
        {"ref_type": "recipe", "ref_id": sub["id"]} for _ in range(8)
    ])  # 8 × 8 = 64 条路径

    resp = client.post(f"/recipes/{recipe['id']}/allergen-cut",
                       json={"allergen": "乳"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # 最小方案 8 条：子配方 8 条目（ENT 编号更小）优于主配方 8 条目
    sub_entry_ids = sorted(e["id"] for e in sub["entries"])
    assert data["entry_ids"] == sub_entry_ids
    assert data["cut_path_count"] == 64
    # 每个条目覆盖 8 条路径，合计去重后 64 条
    assert all(len(e["covered_paths"]) == 8 for e in data["entries"])


def test_allergen_cut_does_not_modify_data(client):
    """整个流程只读：配方与原料数据均不变。"""
    flour, butter, _, cream, cookie = _build_sample(client)
    recipes_before = client.get("/recipes").json()
    cookie_before = client.get(f"/recipes/{cookie['id']}").json()
    cream_before = client.get(f"/recipes/{cream['id']}").json()
    ingredients_before = client.get("/ingredients").json()

    for allergen in ("乳", "麸质", "不存在"):
        resp = client.post(f"/recipes/{cookie['id']}/allergen-cut",
                           json={"allergen": allergen})
        assert resp.status_code == 200, resp.text

    assert client.get("/recipes").json() == recipes_before
    assert client.get(f"/recipes/{cookie['id']}").json() == cookie_before
    assert client.get(f"/recipes/{cream['id']}").json() == cream_before
    assert client.get("/ingredients").json() == ingredients_before


# ---------- 多配方合并备料清单 ----------

def test_material_plan_expands_multiplies_and_merges(client):
    """多层批数相乘、重复引用各计、按原料与单位合并，按编号/单位排序。"""
    flour = make_ingredient(client, "面粉")
    butter = make_ingredient(client, "黄油")
    sugar = make_ingredient(client, "砂糖")
    cream = make_recipe(client, "黄油霜", [
        {"ref_type": "ingredient", "ref_id": butter["id"], "quantity": 250, "unit": "g"},
        {"ref_type": "ingredient", "ref_id": sugar["id"], "quantity": 100, "unit": "g"},
    ])
    # 曲奇：面粉 300g/批 + 黄油霜 2 份/批（同一子配方重复引用，每份都计入）
    cookie = make_recipe(client, "曲奇", [
        {"ref_type": "ingredient", "ref_id": flour["id"], "quantity": 300, "unit": "g"},
        {"ref_type": "recipe", "ref_id": cream["id"], "quantity": 1, "unit": "份"},
        {"ref_type": "recipe", "ref_id": cream["id"], "quantity": 1, "unit": "份"},
    ])

    # 曲奇 2 批：面粉 600；黄油 250×(1+1)×2 = 1000；糖 100×(1+1)×2 = 400
    resp = client.post("/recipes/material-plan", json={
        "recipes": [{"recipe_id": cookie["id"], "batches": 2}],
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["item_count"] == 3
    assert data["items"] == [
        {"ingredient_id": flour["id"], "ingredient_name": "面粉",
         "unit": "g", "quantity": "600"},
        {"ingredient_id": butter["id"], "ingredient_name": "黄油",
         "unit": "g", "quantity": "1000"},
        {"ingredient_id": sugar["id"], "ingredient_name": "砂糖",
         "unit": "g", "quantity": "400"},
    ]


def test_material_plan_multi_recipe_and_deep_chain(client):
    """多个请求配方合并；深层路径批数连乘；同一配方出现多次按各自批数累加。"""
    a = make_ingredient(client, "原料A")
    b = make_ingredient(client, "原料B")
    inner = make_recipe(client, "内馅", [
        {"ref_type": "ingredient", "ref_id": a["id"], "quantity": 2, "unit": "g"},
    ])
    mid = make_recipe(client, "中层", [
        {"ref_type": "recipe", "ref_id": inner["id"], "quantity": 3, "unit": "份"},
        {"ref_type": "ingredient", "ref_id": b["id"], "quantity": 5, "unit": "g"},
    ])
    outer = make_recipe(client, "成品", [
        {"ref_type": "recipe", "ref_id": mid["id"], "quantity": 1.5, "unit": "份"},
    ])

    # 成品 2 批：A = 2×3×1.5×2 = 18；B = 5×1.5×2 = 15
    # 中层 1 批：A += 2×3 = 6（=24）；B += 5（=20）
    # 中层 0.5 批：A += 3（=27）；B += 2.5（=22.5）
    resp = client.post("/recipes/material-plan", json={
        "recipes": [
            {"recipe_id": outer["id"], "batches": 2},
            {"recipe_id": mid["id"], "batches": 1},
            {"recipe_id": mid["id"], "batches": 0.5},
        ],
    })
    assert resp.status_code == 200, resp.text
    items = {(i["ingredient_id"], i["unit"]): i["quantity"]
             for i in resp.json()["items"]}
    assert items[(a["id"], "g")] == "27"
    assert items[(b["id"], "g")] == "22.5"


def test_material_plan_decimal_accumulation_is_exact(client):
    """十进制累加：0.1 + 0.2 必须得到 0.3，而不是浮点误差。"""
    salt = make_ingredient(client, "盐")
    r1 = make_recipe(client, "配方甲", [
        {"ref_type": "ingredient", "ref_id": salt["id"], "quantity": 0.1, "unit": "g"},
    ])
    r2 = make_recipe(client, "配方乙", [
        {"ref_type": "ingredient", "ref_id": salt["id"], "quantity": 0.2, "unit": "g"},
    ])
    resp = client.post("/recipes/material-plan", json={
        "recipes": [
            {"recipe_id": r1["id"], "batches": 1},
            {"recipe_id": r2["id"], "batches": 1},
        ],
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"][0]["quantity"] == "0.3"


def test_material_plan_same_ingredient_different_units_not_converted(client):
    """同一原料不同单位分别成行，不做换算。"""
    milk = make_ingredient(client, "牛奶")
    recipe = make_recipe(client, "奶冻", [
        {"ref_type": "ingredient", "ref_id": milk["id"], "quantity": 200, "unit": "ml"},
        {"ref_type": "ingredient", "ref_id": milk["id"], "quantity": 1, "unit": "kg"},
    ])
    resp = client.post("/recipes/material-plan", json={
        "recipes": [{"recipe_id": recipe["id"], "batches": 1}],
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == [
        {"ingredient_id": milk["id"], "ingredient_name": "牛奶",
         "unit": "kg", "quantity": "1"},
        {"ingredient_id": milk["id"], "ingredient_name": "牛奶",
         "unit": "ml", "quantity": "200"},
    ]


def test_material_plan_missing_quantity_422_lists_entry_ids(client):
    """任一可达条目缺少 quantity -> 422，列出问题条目，不返回部分清单。"""
    a = make_ingredient(client, "原料A")
    b = make_ingredient(client, "原料B")
    sub = make_recipe(client, "子配方", [
        {"ref_type": "ingredient", "ref_id": a["id"], "quantity": 1, "unit": "g"},
        {"ref_type": "ingredient", "ref_id": b["id"]},  # 缺 quantity 与 unit
    ])
    bad_sub_entry = sub["entries"][1]["id"]
    main = make_recipe(client, "主配方", [
        {"ref_type": "recipe", "ref_id": sub["id"]},  # 根条目也缺 quantity/unit
        {"ref_type": "ingredient", "ref_id": a["id"], "quantity": 9, "unit": "g"},
    ])
    bad_root_entry = main["entries"][0]["id"]

    resp = client.post("/recipes/material-plan", json={
        "recipes": [{"recipe_id": main["id"], "batches": 1}],
    })
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "MATERIAL_PLAN_INVALID_ENTRIES"
    assert detail["entry_ids"] == sorted({bad_root_entry, bad_sub_entry})
    assert "items" not in detail  # 不返回部分清单


def test_material_plan_ingredient_without_unit_422(client):
    """原料条目有 quantity 但缺 unit -> 422 并列出该条目。"""
    a = make_ingredient(client, "原料A")
    recipe = make_recipe(client, "配方", [
        {"ref_type": "ingredient", "ref_id": a["id"], "quantity": 10},  # 缺 unit
    ])
    resp = client.post("/recipes/material-plan", json={
        "recipes": [{"recipe_id": recipe["id"], "batches": 1}],
    })
    assert resp.status_code == 422
    assert resp.json()["detail"]["entry_ids"] == [recipe["entries"][0]["id"]]


def test_material_plan_sub_recipe_unit_not_fen_422(client):
    """子配方条目的 unit 不是「份」-> 422；但子树仍照常检查并汇总问题。"""
    a = make_ingredient(client, "原料A")
    sub = make_recipe(client, "子配方", [
        {"ref_type": "ingredient", "ref_id": a["id"]},  # 子树内缺 quantity+unit
    ])
    sub_leaf = sub["entries"][0]["id"]
    main = make_recipe(client, "主配方", [
        {"ref_type": "recipe", "ref_id": sub["id"], "quantity": 1, "unit": "g"},  # 应为「份」
    ])
    resp = client.post("/recipes/material-plan", json={
        "recipes": [{"recipe_id": main["id"], "batches": 1}],
    })
    assert resp.status_code == 422
    assert resp.json()["detail"]["entry_ids"] == sorted(
        {main["entries"][0]["id"], sub_leaf})


def test_material_plan_unknown_recipe_404(client):
    """请求的配方不存在 -> 404（即使列表中还存在有效配方）。"""
    a = make_ingredient(client, "原料A")
    recipe = make_recipe(client, "配方", [
        {"ref_type": "ingredient", "ref_id": a["id"], "quantity": 1, "unit": "g"},
    ])
    resp = client.post("/recipes/material-plan", json={
        "recipes": [
            {"recipe_id": recipe["id"], "batches": 1},
            {"recipe_id": "REC-9999", "batches": 2},
        ],
    })
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert detail["code"] == "RECIPE_NOT_FOUND"
    assert detail["recipe_id"] == "REC-9999"


def test_material_plan_non_positive_batches_422(client):
    """批次数必须为正数：0 或负数被请求校验拒绝（422）。"""
    recipe = make_recipe(client, "空配方", [])
    for bad in (0, -1):
        resp = client.post("/recipes/material-plan", json={
            "recipes": [{"recipe_id": recipe["id"], "batches": bad}],
        })
        assert resp.status_code == 422


def test_material_plan_empty_recipes_rejected(client):
    """至少要给出一个配方。"""
    resp = client.post("/recipes/material-plan", json={"recipes": []})
    assert resp.status_code == 422


def test_material_plan_empty_recipe_gives_empty_items(client):
    """空配方的清单为空。"""
    recipe = make_recipe(client, "空配方", [])
    resp = client.post("/recipes/material-plan", json={
        "recipes": [{"recipe_id": recipe["id"], "batches": 3}],
    })
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"items": [], "item_count": 0}


def test_material_plan_does_not_modify_data(client):
    """备料清单为只读：调用后配方与原料数据均不变。"""
    flour, butter, sugar, cream, cookie = _build_sample(client)
    recipes_before = client.get("/recipes").json()
    cookie_before = client.get(f"/recipes/{cookie['id']}").json()
    cream_before = client.get(f"/recipes/{cream['id']}").json()
    ingredients_before = client.get("/ingredients").json()

    resp = client.post("/recipes/material-plan", json={
        "recipes": [
            {"recipe_id": cookie["id"], "batches": 2},
            {"recipe_id": cream["id"], "batches": 1.5},
        ],
    })
    assert resp.status_code == 200, resp.text

    assert client.get("/recipes").json() == recipes_before
    assert client.get(f"/recipes/{cookie['id']}").json() == cookie_before
    assert client.get(f"/recipes/{cream['id']}").json() == cream_before
    assert client.get("/ingredients").json() == ingredients_before
