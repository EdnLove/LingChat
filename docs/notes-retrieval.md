# 笔记语义检索落地实施文档

> 面向下一个接手实现的 agent。本文档是**实施规格**，不是研究提案；评测结论已经定稿，按第 2 节的配置实现即可。
> 评测环境在仓库外的 `~/m2v`，相关脚本：`eval/notes_eval_set.py`、`eval/eval_project_onnx.py`、`eval/eval_strategies.py`。
> 日期：2026-10-18

## 0. 一句话任务

给 `memory_get_notes` 增加可选的 `query` 参数：有 query 时用现有 `EmbeddingEngine` 做纯向量检索并返回 Top-K（默认 8）；没有 query 时保持现在的全量返回。文档侧编码 `content + tags`，查询侧裸文本。**不做 RRF、不做门控追加、不加 bge 前缀、不换模型。**

## 1. 背景

`memory_get_notes` 当前把某个角色的全部手动笔记一次性返回给 LLM。笔记量小的时候没问题，但随笔记增长会挤占上下文。离线评测已经验证：同一套 bge-small-zh 模型下，纯向量召回明显优于词法基线，任何固定权重的 RRF 融合都会降低 MRR。

产品形态上，笔记检索是"AI 主动按需取回"，不是独立的搜索页面，因此实现要尽量轻：复用现有引擎、进程内缓存、失败回退全量。

## 2. 评测结论（已定稿，不要再重复推导）

### 2.1 数据与模型

- 评测集：400 条短句笔记 + 120 条查询（semantic 47 / mixed 30 / lexical 28 / multi 15）。
- 模型：`data/third_party/embedding_model_bge_small_zh/model.onnx`，bge-small-zh-v1.5 uint8 量化，512 维，约 23MB。
- 该文件与 m2v 评测用的 Xenova uint8 导出**逐字节一致**，量化在这个任务上没有可见精度损失。
- 查询侧不加 bge 指令前缀（实测加前缀反而略差）。

### 2.2 最终指标（400/120，CPU）

| 策略                             | R@1          | R@3   | R@5       | R@8       | R@10      | MRR       |
| -------------------------------- | ------------ | ----- | --------- | --------- | --------- | --------- |
| 仅 content（旧文本表示）         | 0.606        | 0.750 | 0.812     | 0.851     | 0.851     | 0.762     |
| **content + tags（采用）**       | 0.613        | 0.801 | **0.842** | **0.867** | **0.878** | **0.786** |
| content + tags + 分数融合 α=0.02 | 0.621        | 0.806 | 0.838     | 0.869     | 0.876     | 0.793     |
| content + tags + 门控追加        | 0.613        | 0.801 | 0.842     | 0.867     | 0.871     | 0.783     |
| content + tags + RRF 固定权重    | 均低于纯向量 |       |           |           |           |           |

结论：

1. **文档侧拼 tags 是最大收益**，四个查询类型分桶全部提升（mixed R@5 0.844→0.900，semantic 0.745→0.777）。
2. **纯向量排序最优**。RRF、门控追加都没有净收益；分数融合 α=0.02 只有 +0.007 MRR，不值得为此维护词法分数归一化和调参。
3. **Top-8 有实际价值**，multi 查询 R@8 0.700 高于 R@5 0.667；Top-10 再多约 1 个点覆盖。
4. 词法兜底只在引擎不可用时作为降级手段，不参与在线排序。

## 3. 现状盘点

| 位置                                                     | 现状                                                                                                                                                                                      |
| -------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `crates/ling-chat-notes/src/lib.rs`                      | `NotesStore` 提供 `load/save/add/update/delete`，按角色读写 `<data_dir>/game_data/notes/<角色>.json`；无缓存、无索引                                                                      |
| `crates/ling-chat-notes/src/embedding/engine.rs`         | `EmbeddingEngine::load` / `is_enabled` / `embed(&str)`；CLS pooling + L2 归一化；Session 惰性创建，内部 `Mutex`，同步阻塞；模型目录常量 `MODEL_DIR_NAME = "embedding_model_bge_small_zh"` |
| `crates/ling-chat-main/src/ai_service/tools/memory.rs`   | `GetNotes` 工具，参数只有 `role`，执行时 `NotesStore::new(data_dir())` 全量 load 并原样返回                                                                                               |
| `crates/ling-chat-main/src/state.rs`                     | `AppState.embedding: Arc<EmbeddingEngine>`，与 `ToolRag` 共用同一个实例                                                                                                                   |
| `src-tauri/src/app/setup/build.rs`                       | 启动时构造 `EmbeddingEngine`，同步加载 tokenizer，ONNX Session 延迟到首次 embed                                                                                                           |
| `crates/ling-chat-main/src/ai_service/tools/executor.rs` | 工具默认执行超时 **2 秒**；可用 `timeout_hint()` 覆盖                                                                                                                                     |
| `crates/ling-chat-main/src/ai_service/tools/tool_rag.rs` | 已有"按指纹缓存向量 + `spawn_blocking` 推理 + 锁不跨 await"的成熟模式，直接参考                                                                                                           |

## 4. 实现方案

### 4.1 数据流

```
memory_get_notes(query, top_k, role)
  ├─ query 缺失 / 空白            → 保持现状：返回全部 notes
  ├─ engine 未启用或推理失败      → 降级：返回全部 notes，warn 日志
  └─ 正常检索
       ├─ NotesStore::load(role) 得到 notes
       ├─ 计算文本 = content + " " + tags.join(" ")
       ├─ 查进程内向量缓存，找出缺失/指纹变化的条目
       ├─ spawn_blocking: 批量嵌入缺失条目 + 嵌入 query
       ├─ 写回缓存（锁不跨 await / 推理）
       └─ 点积排序，取 top_k，返回 Note 数组 + 元信息
```

### 4.2 工具参数与返回

`definition()` 增加两个可选参数，保持 `additionalProperties: false`：

```json
{
  "role": { "type": "string", "description": "要读取其笔记的角色名；不传时读取当前角色" },
  "query": {
    "type": "string",
    "description": "检索关键词或简短问句；不传则返回全部笔记。建议用陈述性关键词（如「食物过敏」「睡前约定」）"
  },
  "top_k": { "type": "integer", "minimum": 1, "maximum": 20, "description": "返回条数，默认 8" }
}
```

返回结构向后兼容：

```json
{
  "ok": true,
  "notes": [{ "id": "...", "content": "...", "tags": ["..."], "created_at": "..." }],
  "total": 137,
  "returned": 8,
  "retrieved": true
}
```

- `notes` 元素形状**不要改**，否则会影响现有提示词和写操作工具的使用习惯。
- 不传 query 时返回 `{"ok": true, "notes": [...]}`（或同样带 `retrieved: false`，但必须保证 `notes` 是全量）。
- 评分不返回给模型，调试时用 `tracing::debug!` 打 top 分数即可。

### 4.3 文本表示

```rust
fn note_text(note: &Note) -> String {
    let tags = note.tags.join(" ");
    if tags.is_empty() {
        note.content.clone()
    } else {
        format!("{} {}", note.content.trim(), tags)
    }
}
```

query 直接用 `query.trim()`，不加任何前缀。query 为空按"未传"处理。

### 4.4 向量缓存

笔记量小（通常几十到几百条），暴力点积足够，**不要引入 ANN 索引或持久化向量库**。

建议在 `GetNotes` 结构体里放缓存（该工具在 `built_in_registry` 中以 `Arc::new` 注册，是进程内单例）：

```rust
// 键包含角色，避免不同角色的同名 note id 冲突
type CacheKey = (String, String);          // (role, note_id)
type CacheEntry = (u64, Arc<[f32]>);       // (指纹, 归一化向量)

struct GetNotes {
    cache: RwLock<HashMap<CacheKey, CacheEntry>>,
}
```

- 指纹 = `note_text` 的哈希（`std::collections::hash_map::DefaultHasher` 足够）。内容或 tags 变化时自动失效，与 `ToolRag` 的策略一致。
- 每次 load 后顺手清理当前角色已不存在的 id，防止缓存无限增长。
- 读取缓存时 clone 出 `Arc<[f32]>`，推理期间不要持有 `RwLock`。
- 不跨 `await` 持有标准库锁。

如果后续持久记忆也要复用，可以把缓存提升到 `AppState`，但本次不需要，避免改动组合根。

### 4.5 推理与超时（关键）

现有 `EmbeddingEngine::embed` 是**单条同步调用**，且内部 `Mutex<Session>` 串行。首次检索要为最多几百条笔记建向量，逐条推理可能超过工具默认的 2 秒超时。

两种做法，优先第一种：

1. **给引擎加批量接口**（推荐）：`embed_batch(&[String]) -> Result<Vec<Vec<f32>>>`
   - tokenizer 配置 padding（`PaddingParams`，pad id/token 与模型一致），batch 内按最长序列补齐。
   - 一次 `session.run`，输出 `[B, seq, dim]`，对每条取 CLS 行并 L2 归一化。
   - batch size 16–32。400 条通常在 1 秒左右完成。
2. **不批量**：在一个 `spawn_blocking` 里循环调用 `embed`，然后给 `GetNotes` 加
   `fn timeout_hint(&self) -> Option<Duration> { Some(Duration::from_secs(15)) }`。
   - 可行但首次体验差，且会和 Tool RAG 抢 Session 锁。
   - 如果选这条，超时提示必须加，否则首次检索大概率直接超时。

无论哪种，**推理必须放在 `spawn_blocking`**；工具执行是 async 的。

缓存命中后，每次检索只需要编码 1 条 query（几毫秒），不再需要放宽超时。

### 4.6 降级

- `state.embedding.is_enabled() == false`：返回全量，`warn` 一次即可（不要每次刷日志）。
- `embed`/`embed_batch` 返回错误：返回全量，不要 panic，不要把错误抛给模型。
- 笔记为空：返回空数组。
- 非法 `top_k`：clamp 到 1..=20；非数字按默认 8 处理。

降级时行为与旧版完全一致，这是硬要求。

### 4.7 工具描述

更新 `memory_get_notes` 的描述，明确"有 query 时按语义检索、不传时返回全部"，并提示模型 query 用简短关键词而不是整段对话。描述会进入 Tool RAG 的语料，改动后工具检索的分数会轻微变化，属于预期。

## 5. 改动清单

| 文件                                                    | 改动                                                                                          |
| ------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| `crates/ling-chat-notes/src/embedding/engine.rs`        | 新增 `embed_batch`（若选方案 1）；复用现有 tokenizer/Session/CLS pooling/L2 归一化代码        |
| `crates/ling-chat-notes/src/retrieval.rs`（新建，可选） | 放纯函数：`note_text`、余弦点积排序、Top-K；便于单测，不依赖 tauri                            |
| `crates/ling-chat-main/src/ai_service/tools/memory.rs`  | `GetNotes` 增加字段与构造器、参数解析、缓存、`spawn_blocking`、降级、`timeout_hint`（若需要） |
| `crates/ling-chat-main/src/ai_service/tools/mod.rs`     | 注册处改为 `Arc::new(GetNotes::new())`（当前是 `Arc::new(GetNotes)`）                         |
| `docs/notes-retrieval.md`                               | 本文档；实现完成后补一节"最终实现与偏差"                                                      |

不需要改前端、命令注册、capabilities、数据库或数据格式。

## 6. 验收标准

### 6.1 自动化

```bash
cargo test -p ling-chat-notes
cargo check --workspace
cargo test --workspace
```

要求：

- 新增单元测试覆盖：`note_text` 拼 tags、空 tags、点积排序、Top-K 截断、`top_k` clamp、缓存指纹变化失效。
- 降级路径测试：引擎禁用时 `GetNotes` 返回全量（可以通过构造函数注入一个禁用态引擎，或把检索逻辑抽成纯函数后测降级分支）。
- 真实模型 smoke 测试标 `#[ignore]`，需要模型文件时手动跑；不要放进默认 CI。

### 6.2 手工验证

1. 不传 query：返回该角色全部笔记，与改动前一致。
2. `query="我对什么食物过敏"`：花生、海鲜两条过敏笔记进入前 3。
3. `query="睡不着怎么办"`：失眠/白噪音类笔记命中。
4. 给一条笔记改 tags 后立即再查：新 tags 生效（缓存已失效）。
5. 临时重命名模型文件：检索降级为全量，应用不崩、无 panic。
6. 400 条笔记的测试数据下：首次检索 < 5 秒，第二次 < 200 毫秒。

### 6.3 指标回归（可选但推荐）

把 `~/m2v/eval/notes_eval_set.json`（400/120）复制为测试 fixture，写一个 `#[ignore]` 的真实模型回归测试，断言：

- 向量 MRR ≥ 0.75
- R@5 ≥ 0.82

阈值留出余量，避免因查询集微调导致测试脆弱。这个测试用于防止后续改动把文本表示或池化方式改坏。

## 7. 数据与分发

- 模型已经在 `data/third_party/embedding_model_bge_small_zh/`，**本次不需要新增任何模型文件**，也不要改 `MODEL_DIR_NAME`。
- `data/third_party/notes_embedding/` 是评测环境留下的重复副本（24MB，无代码引用，未跟踪），会随打包脚本一起进安装包。建议删除，但这是 `data/` 下的未跟踪文件，删除前先向维护者确认。
- 桌面端：`prepare-desktop-resources.mjs` 整目录递归复制 `data/third_party/`，模型自动进安装包。
- 移动端：模型随 `data.7z` 解压。本次不新增文件，不受 `.seeded` 不重新解压的影响；以后换模型版本才需要处理升级播种。
- 不要把模型放进 git（`.gitignore` 已排除 `data/**`）。

## 8. 明确不做

- 不做 RRF、权重扫描、门控追加。
- 不加 bge query 前缀。
- 不换 embedding 模型，不做 Model2Vec 蒸馏。
- 不建 ANN/HNSW、不做持久化向量索引。
- 不返回相似度分数给模型。
- 不改 `Note` 的 JSON 结构和笔记文件格式。
- 不做前端笔记管理页面。

## 9. 风险

| 风险                       | 处理                                                                                         |
| -------------------------- | -------------------------------------------------------------------------------------------- |
| 首次批量嵌入超过工具超时   | 加 `embed_batch`，并按需设置 `timeout_hint`（见 4.5）                                        |
| 与 Tool RAG 抢 Session 锁  | 两者共用 `state.embedding`；批量后单次持锁时间更短。不要并发创建第二个引擎实例               |
| Windows 缺 onnxruntime.dll | `EmbeddingEngine` 已经是禁用态，按 4.6 降级为全量                                            |
| 笔记文件被外部修改         | 缓存按内容指纹失效，每次检索都重新 `load`，无需文件监听                                      |
| 检索结果偏离预期           | 先看日志里的 top 分数；再回 `~/m2v` 用同一模型跑 `eval_strategies.py` 复核，不要凭感觉调阈值 |

## 10. 实现完成后

在本文件末尾追加"最终实现与偏差"一节，记录：

- 实际采用单条还是批量嵌入，首次/缓存后耗时。
- 缓存放置位置与失效策略。
- 是否保留 `timeout_hint`。
- 手工验证和测试的实际结果。
- 与本文档不一致的地方及原因。
