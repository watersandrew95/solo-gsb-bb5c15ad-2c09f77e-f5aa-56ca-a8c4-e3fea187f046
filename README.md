# 食品工坊配方标签核查 API

供小型食品工坊使用的配方标签核对服务：登记原料及其过敏原，用**带独立条目编号**的原料或子配方组成配方；保存配方时校验引用与循环；查询时展开全部引用路径并按过敏原汇总每条来源路径，用于成品标签核对。改动配方前还可用**标签预演**接口，在不写入数据库的前提下预演整体替换后的过敏原变化，判断是否需要重印标签；需要剔除某过敏原时，可用**过敏原截断**接口算出最少需要停用哪些条目（只读，给出方案但不改动数据）；生产排期时可用**合并备料清单**接口，一次提交多个配方及各自批次数，沿引用逐层展开、批数相乘，按原料与单位汇总采购用量（只读）。

技术栈：Python 3.12 · FastAPI · SQLAlchemy 2 · SQLite

## 一条命令启动

```bash
docker compose up --build
```

依赖安装随镜像构建完成，SQLite 数据库（建目录、建表）随应用启动自动初始化，无需任何手工步骤。

启动后访问：

| 地址 | 说明 |
|---|---|
| http://localhost:8000 | 服务信息 |
| http://localhost:8000/docs | Swagger 交互式文档（可直接试调用） |
| http://localhost:8000/health | 健康检查 |

## 配置

| 配置项 | 默认值 | 说明 |
|---|---|---|
| 访问端口 | `8000` | 改 `docker-compose.yml` 中 `ports: ["8080:8000"]` 的**前一个**端口即可换访问端口 |
| `DATABASE_URL` | 容器内 `sqlite:////data/labels.db`；本地运行 `sqlite:///./data/labels.db` | SQLAlchemy 连接串，SQLite 文件目录会自动创建 |
| 数据持久化 | named volume `label-data` → `/data` | 删除容器不丢数据 |

本地开发（不用 Docker）：

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000   # 启动时自动初始化 ./data/labels.db
pytest                                       # 运行测试
```

## 编号规则

| 编号 | 格式 | 说明 |
|---|---|---|
| 原料 | `ING-0001` | 登记时自动分配 |
| 配方 | `REC-0001` | 创建时自动分配 |
| 条目 | `ENT-000001` | 配方中每一行组成的**独立条目编号**，全局唯一；同一子配方被使用多次时，每次使用各有自己的条目编号 |

## 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/ingredients` | 登记原料及过敏原（过敏原自动去重去空白） |
| GET | `/ingredients` / `/ingredients/{id}` | 查询原料 |
| POST | `/recipes` | 新建配方（条目按顺序组成，可引用原料或子配方） |
| PUT | `/recipes/{id}` | 整体替换配方组成 |
| POST | `/recipes/{id}/label-preview` | **标签预演**：接收与整体替换相同的请求，校验相同但**不写入数据库**，返回替换前后的过敏原变化 |
| POST | `/recipes/{id}/allergen-cut` | **过敏原截断**：求最少需要停用哪些条目才能截断含目标过敏原的全部原料路径（只读，不写入） |
| POST | `/recipes/material-plan` | **合并备料清单**：多个配方 × 各自正数批次数，逐层展开、批数相乘，按原料与单位十进制汇总用量（只读） |
| GET | `/recipes` / `/recipes/{id}` | 查询配方及其条目 |
| GET | `/recipes/{id}/expansion` | **标签核对**：展开全部引用路径，按过敏原汇总来源 |

## 保存规则（重点）

保存配方（POST / PUT）时：

1. **引用必须存在**：条目引用了不存在的原料或子配方 → `400 UNKNOWN_REFERENCE`，返回全部缺失引用；
2. **拒绝循环**：新组成会导致配方直接或间接引用回自身 → `409 CYCLE_DETECTED`，返回循环路径；
3. **失败零改动**：所有校验通过后才写入，删除旧条目与写入新条目在同一事务提交——任何校验失败都不会改动数据库中的原配方。

`POST /recipes/{id}/label-preview` 沿用完全相同的校验（引用存在性、循环、名称冲突），校验失败时返回与保存接口一致的错误；校验通过时只做展开与对比，**不写入任何数据**。

## 标签预演（判断是否重印标签）

`POST /recipes/{id}/label-preview` 接收与整体替换（PUT）相同的请求体，分别展开**当前配方**与**预演配方**的全部原料路径，按过敏原对比来源后返回三组结果：

| 字段 | 含义 |
|---|---|
| `added_allergens` | 替换后新增的过敏原（`before` 为空） |
| `removed_allergens` | 替换后移除的过敏原（`after` 为空） |
| `changed_allergens` | 仍存在、但来源原料或路径发生变化的过敏原 |

每项都列出变更前后的来源原料与路径（`before` / `after`），便于判断是否需要重印标签。路径标识由**每层条目位置**依次组成（如 `[2, 1]` 表示「本配方第 2 行 → 子配方第 1 行」）；同一子配方被重复引用时，每次引用位置不同，各自保留为不同来源。

## 过敏原截断（最少停用方案）

`POST /recipes/{id}/allergen-cut` 接收目标过敏原（`{"allergen": "乳"}`），复用展开结果取出全部**含该过敏原的原料路径**，求出最少需要停用哪些条目，才能使这些路径全部被截断。**只读**接口：只返回方案，不改动任何配方或原料数据。

规则：

1. **条目即修改单位**：条目编号全局唯一，同一子配方被多处引用时，其子条目出现在每条引用路径中——停用该条目一次，即覆盖它的所有引用实例；
2. **全局最小命中集**：在「需要截断的路径总数 ≤ 64 且候选条目数 ≤ 24」时求全局最优（停用条目数最少）；同样数量时，按升序条目编号列表的字典序取最小方案；
3. **超限拒绝**：超过上述规模返回 `422 CUT_PROBLEM_TOO_LARGE`，并在响应中给出实际规模（`path_count` / `candidate_count`）与上限（`max_paths` / `max_candidates`）；
4. **空路径集合**（配方不含该过敏原）返回空方案：`entry_ids` 与 `entries` 为空、`cut_path_count` 为 0；
5. 配方不存在返回 `404 RECIPE_NOT_FOUND`。

响应字段：

| 字段 | 含义 |
|---|---|
| `entry_ids` | 最少需要停用的条目编号（升序） |
| `entries` | 每个待停用条目覆盖（截断）的原料路径：条目编号序列 + 来源原料 |
| `cut_path_count` | 被截断路径总数（一条路径被多个条目覆盖时只计一次） |

## 合并备料清单（多配方 × 多批次，只读）

`POST /recipes/material-plan` 接收多个配方编号及各自的**正数批次数**，沿现有配方引用逐层展开，合并成一张备料清单：

1. **批数逐层相乘**：子配方条目的 `quantity` 表示「每批使用的子配方批数」，路径上各层批数依次相乘；例如主配方 2 批、子配方每份用量 1.5 份、原料每份 100 g，则该原料用量为 `2 × 1.5 × 100 = 300 g`；
2. **重复引用分别计入**：同一子配方被同一配方引用多次时，每条引用独立展开、各自的批数都要计入；请求中同一配方出现多次也按各自批次数累加；
3. **按原料与单位分组**：用量按「原料编号 + unit」分组，以**十进制（Decimal）累加**并以**字符串**返回（如 `"0.3"` 而非 `0.30000000000000004`），结果按原料编号、再按单位排序；**不同单位不做任何换算**，同一原料的 `kg` 与 `g` 各占一行；
4. **整体校验，不出部分清单**：任一可达条目缺少 `quantity`、原料条目缺少 `unit`，或子配方条目的 `unit` 不是「份」，返回 `422 MATERIAL_PLAN_INVALID_ENTRIES`，并在 `entry_ids` 中**列出全部问题条目编号**（升序去重），响应不含任何用量行；
5. **只读**：只读取现有配方与原料数据，不写入、不修改任何内容；
6. **配方不存在**：请求中的任一配方编号不存在时返回 `404 RECIPE_NOT_FOUND`，响应给出首个不存在的编号；批次数必须为正数、配方列表至少一项，否则返回 `422`。

响应字段：

| 字段 | 含义 |
|---|---|
| `items` | 合并后的用量行：`ingredient_id`、`ingredient_name`、`unit`、`quantity`（十进制字符串） |
| `item_count` | 用量行数量 |

## 使用示例

```bash
B=http://localhost:8000

# 1. 登记原料
curl -X POST $B/ingredients -H 'Content-Type: application/json' \
  -d '{"name":"黄油","allergens":["乳"]}'          # -> ING-0001
curl -X POST $B/ingredients -H 'Content-Type: application/json' \
  -d '{"name":"面粉","allergens":["麸质"]}'        # -> ING-0002
curl -X POST $B/ingredients -H 'Content-Type: application/json' \
  -d '{"name":"砂糖","allergens":[]}'              # -> ING-0003

# 2. 子配方：黄油霜
curl -X POST $B/recipes -H 'Content-Type: application/json' -d '{
  "name":"黄油霜",
  "entries":[
    {"ref_type":"ingredient","ref_id":"ING-0001","quantity":250,"unit":"g"},
    {"ref_type":"ingredient","ref_id":"ING-0003","quantity":100,"unit":"g"}
  ]}'                                            # -> REC-0001（条目 ENT-000001/2）

# 3. 主配方：曲奇（同一子配方使用两次，各自保留条目编号）
curl -X POST $B/recipes -H 'Content-Type: application/json' -d '{
  "name":"曲奇",
  "entries":[
    {"ref_type":"ingredient","ref_id":"ING-0002","quantity":300,"unit":"g"},
    {"ref_type":"recipe","ref_id":"REC-0001","quantity":1,"unit":"份"},
    {"ref_type":"recipe","ref_id":"REC-0001","quantity":1,"unit":"份"}
  ]}'                                            # -> REC-0002（条目 ENT-000003/4/5）

# 4. 标签核对：展开全部路径 + 过敏原来源汇总
curl $B/recipes/REC-0002/expansion

# 5. 标签预演：打算把曲奇改成「黄油霜×1 + 花生酱」，先看标签变化（不写入）
curl -X POST $B/ingredients -H 'Content-Type: application/json' \
  -d '{"name":"花生酱","allergens":["花生"]}'        # -> ING-0004
curl -X POST $B/recipes/REC-0002/label-preview -H 'Content-Type: application/json' -d '{
  "name":"曲奇",
  "entries":[
    {"ref_type":"recipe","ref_id":"REC-0001","quantity":1,"unit":"份"},
    {"ref_type":"ingredient","ref_id":"ING-0004","quantity":50,"unit":"g"}
  ]}'

# 6. 过敏原截断：要剔除「乳」，最少需要停用哪些条目？（只读，不写入）
curl -X POST $B/recipes/REC-0002/allergen-cut -H 'Content-Type: application/json' \
  -d '{"allergen":"乳"}'

# 7. 合并备料清单：曲奇 2 批 + 黄油霜 1 批，各自批次数逐层相乘后合并（只读）
curl -X POST $B/recipes/material-plan -H 'Content-Type: application/json' -d '{
  "recipes":[
    {"recipe_id":"REC-0002","batches":2},
    {"recipe_id":"REC-0001","batches":1}
  ]}'
```

> 第 7 步中：面粉 `300×2 = 600g`；黄油霜被曲奇每条引用各 1 份、2 批再加独立 1 批，共 5 份 → 黄油 `250×5 = 1250g`、砂糖 `100×5 = 500g`。

第 7 步的响应（数量按原料编号、单位排序，十进制字符串）：

```json
{
  "items": [
    {"ingredient_id": "ING-0001", "ingredient_name": "黄油", "unit": "g", "quantity": "1250"},
    {"ingredient_id": "ING-0002", "ingredient_name": "面粉", "unit": "g", "quantity": "600"},
    {"ingredient_id": "ING-0003", "ingredient_name": "砂糖", "unit": "g", "quantity": "500"}
  ],
  "item_count": 3
}
```

若某条可达条目无法参与计算（如子配方条目的 `unit` 写成了 `"g"` 而非 `"份"`），响应为 422 并列出全部问题条目，不返回部分清单：

```json
{
  "detail": {
    "code": "MATERIAL_PLAN_INVALID_ENTRIES",
    "message": "存在无法参与备料计算的条目：条目缺少 quantity、原料条目缺少 unit，或子配方条目的 unit 不是「份」",
    "entry_ids": ["ENT-000004"],
    "entry_count": 1
  }
}
```

展开结果中，每条来源路径由条目编号依次记录（如 `["ENT-000004","ENT-000001"]` 表示「曲奇第 2 行 → 黄油霜第 1 行 → 黄油」）；`allergen_summary` 按过敏原分组，同一子配方使用两次会列出两条独立来源路径：

```json
"allergen_summary": [
  {"allergen": "乳", "sources": [
    {"path": ["ENT-000004", "ENT-000001"], "ingredient_id": "ING-0001", "ingredient_name": "黄油"},
    {"path": ["ENT-000005", "ENT-000001"], "ingredient_id": "ING-0001", "ingredient_name": "黄油"}
  ]},
  {"allergen": "麸质", "sources": [
    {"path": ["ENT-000003"], "ingredient_id": "ING-0002", "ingredient_name": "面粉"}
  ]}
]
```

第 5 步的预演响应如下（数据库中的曲奇配方保持不变）：

```json
{
  "recipe_id": "REC-0002",
  "recipe_name": "曲奇",
  "preview_name": "曲奇",
  "added_allergens": [
    {"allergen": "花生", "before": [], "after": [
      {"ingredient_id": "ING-0004", "ingredient_name": "花生酱", "path": [2]}
    ]}
  ],
  "removed_allergens": [
    {"allergen": "麸质", "before": [
      {"ingredient_id": "ING-0002", "ingredient_name": "面粉", "path": [1]}
    ], "after": []}
  ],
  "changed_allergens": [
    {"allergen": "乳",
     "before": [
       {"ingredient_id": "ING-0001", "ingredient_name": "黄油", "path": [2, 1]},
       {"ingredient_id": "ING-0001", "ingredient_name": "黄油", "path": [3, 1]}
     ],
     "after": [
       {"ingredient_id": "ING-0001", "ingredient_name": "黄油", "path": [1, 1]}
     ]}
  ]
}
```

即：新增「花生」、移除「麸质」；「乳」仍在，但来源从两条路径（黄油霜被引用两次）变为一条——标签需要重印。确认无误后再用 `PUT /recipes/REC-0002` 正式保存同样的请求体。

第 6 步的截断方案如下——两条含「乳」路径都经过黄油霜中的黄油条目 `ENT-000001`，停用它一次即可覆盖全部引用实例（数据库中的配方保持不变）：

```json
{
  "recipe_id": "REC-0002",
  "recipe_name": "曲奇",
  "allergen": "乳",
  "entry_ids": ["ENT-000001"],
  "entries": [
    {"entry_id": "ENT-000001", "covered_paths": [
      {"path": ["ENT-000004", "ENT-000001"], "ingredient_id": "ING-0001", "ingredient_name": "黄油"},
      {"path": ["ENT-000005", "ENT-000001"], "ingredient_id": "ING-0001", "ingredient_name": "黄油"}
    ]}
  ],
  "cut_path_count": 2
}
```

## 项目结构

```
app/
├── main.py            # 应用入口，启动时初始化数据库
├── database.py        # 引擎 / 会话 / 建表（DATABASE_URL 可配置）
├── models.py          # Ingredient / Recipe / RecipeEntry / 发号器
├── schemas.py         # 请求与响应模型
├── services.py        # 校验（引用、循环）、原子保存、展开汇总、标签预演对比、过敏原截断最小命中集
└── routers/           # /ingredients、/recipes 路由
tests/test_api.py      # 41 个行为测试（校验、循环、原子性、展开、标签预演、过敏原截断、合并备料清单）
Dockerfile             # python:3.12-slim，构建时安装依赖
docker-compose.yml     # 一条命令启动，端口与数据卷在此配置
```
