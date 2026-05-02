# Obsidian RAG Protocol (ORP)

> 用 Obsidian 知识库给 AI Agent 装上长期记忆——零额外 Token、增量索引、双向协作。

---

## 目录

- [这是什么](#这是什么)
- [为什么做这个](#为什么做这个)
- [协议概览](#协议概览)
- [核心设计决策](#核心设计决策)
- [自动上下文注入](#自动上下文注入)
- [双向协作](#双向协作)
- [Skill ↔ Vault 自动展开](#skill--vault-自动展开)
- [参考实现](#参考实现)
- [索引 Schema](#索引-schema)
- [自动注入规则](#自动注入规则)
- [状态](#状态)
- [License](#license)
- [关注作者](#关注作者)

---

## 这是什么

Obsidian RAG Protocol（ORP）是一个**开放协议**，让任何拥有文件系统访问权限的 AI Agent 都能把你的 Obsidian Vault 当作**持久化知识层**——相当于给 Agent 装上了你多年积累的"第二大脑"。

它**不是** Obsidian 插件，也不是一次性脚本。ORP 定义了一套 Agent 如何读取、索引、同步 Obsidian 笔记的规范，并提供 Python 参考实现，可供任何 Agent 集成。

核心特性：

- **零 Token 开销**——上下文注入不消耗额外 Prompt Token
- **增量索引**——基于 SHA256 内容哈希，跳过未变更文件，扛得住 macOS iCloud 同步的脏时间戳
- **双向协议**——多个 Agent 可共享同一个 Vault，各写各的目录
- **人机两读**——索引文件既是 JSON，也能被人类一目了然地阅读

---

## 为什么做这个

我叫 Vincent，Crypto/Web3 营销人，不是职业开发者。

我的 Obsidian Vault 里攒了好几年的笔记和研究——从项目复盘、行业分析到个人成长记录。但我每天和 AI Agent 对话时，它总是"失忆"：每次新会话都要从零开始，反复解释同样的背景。

我突然想：为什么不让 Agent 直接读取我的 Vault？笔记本身就是我最好的上下文。于是我一边学 Python，一边从自己的真实需求出发，迭代出了这套协议。没有大厂背景，没有工程师团队，就是一个想要解决自己痛点的人，亲手把方案做了出来。

我选择把它开源，因为我相信：**最好的协议是那些解决真实痛点的协议**，而我的痛点一定也是很多人的痛点。

---

## 协议概览

ORP 的架构可以用以下流程描述：

1. **Vault 扫描**：`rebuild-vault-index.py` 按配置的子目录扫描 Vault 中的 `.md` 文件，逐个提取 YAML frontmatter 元数据（title, aliases, summary_points, last_action, status, author 等）。
2. **内容哈希**：对每个文件的原始字节计算 SHA256 哈希值，作为文件内容的唯一指纹。
3. **增量比对**：将本次哈希与上一次索引中的哈希进行比对——哈希相同则跳过提取，直接沿用旧索引中的元数据（包括人工编辑过的 aliases）；哈希不同则重新提取。
4. **索引输出**：所有条目汇总写入 `vault-index.json`，一条记录对应一个笔记文件。
5. **Agent 查询**：Agent 在收到非平凡问题时，读取 `vault-index.json`，对用户关键词与 `aliases` 字段做模糊匹配，命中后自动读取对应 Vault 文件，完成上下文注入。

整个过程的关键在于：**Agent 只需要一次简单的文件读取操作就拿到索引，无需额外的 LLM 推理或 Token 消耗。**

---

## 核心设计决策

| 决策 | 理由 |
|------|------|
| **SHA256 内容哈希，而非 mtime** | 文件时间戳在 macOS/iCloud 同步下不可靠；内容哈希是确定性的，同一内容永远产出同一哈希。 |
| **别名按优先级解析** | Frontmatter > 手工映射表 > 上一次索引 > 自动回退值。保证人工维护的别名永远优先于机器生成的。 |
| **基于路径排除，而非 frontmatter 标记** | 目录模式是确定性的，而 `rag_exclude: true` 容易被忘记或遗漏。路径规则一次配好，全局生效。 |
| **默认增量** | 同一哈希跳过提取，节省 CPU 并保留人工对别名的编辑成果。 |
| **Frontmatter 驱动摘要** | 从 YAML 中读取 `summary_points` 和 `last_action`，纯文本作为降级方案，确保摘要质量可控。 |
| **JSON 索引，而非 LLM 推理** | 读取 `vault-index.json` 就是一次工具调用，零 Prompt Token 开销。 |

---

## 自动上下文注入

从 v3.4 开始，ORP 支持自动上下文注入，流程如下：

1. **检测**：Agent 判断用户的问题是否为"非平凡问题"（需要背景知识才能回答的问题）。
2. **读取索引**：Agent 调用文件读取工具加载 `vault-index.json`。
3. **模糊匹配**：将用户关键词与索引中每条记录的 `aliases` 字段进行匹配。
4. **命中注入**：若匹配成功，Agent 自动读取对应的 Vault 原文文件，将其作为上下文注入当前对话。
5. **未命中则跳过**：不注入任何内容，对话照常进行。

**核心优势**：整个过程不需要额外的 LLM 推理，不需要消耗 Prompt Token——只是一次普通文件读取。

---

## 双向协作

ORP 天然支持多 Agent 共享一个 Vault：

- **Agent A** 写入 `vault/hermes/` 目录
- **Agent B** 写入 `vault/curator/` 目录
- 两者共享同一份 `vault-index.json`

每个 Agent 拥有自己的写入空间，读取时可以访问全 Vault。这意味着一个 Agent 产出的笔记，另一个 Agent 可以立刻作为上下文使用——真正实现了**双向知识流动**。

---

## Skill ↔ Vault 自动展开

这也是 v3.4 引入的特性，形成一个从"技能"到"上下文"的自动链路：

1. Agent 的某个 Skill 引用了 Vault 中的某个文件路径。
2. ORP 协议自动读取该引用文件。
3. 文件内容被自动注入到当前对话的上下文中。

**效果**：Skill 定义不需要手动复制笔记内容，只需引用路径，上下文会自动展开——**从 Skill 到 Vault 到 Context，一条链自动打通，无需手动搬运。**

---

## 参考实现

`rebuild-vault-index.py` 是 ORP 的参考实现，约 150 行 Python 代码，核心功能包括：

- 扫描指定 Obsidian Vault 子目录下的 `.md` 文件
- 提取 YAML frontmatter 中的元数据（title, aliases, summary_points, last_action, status, author）
- 计算每个文件的 SHA256 内容哈希
- 支持增量更新——哈希未变则跳过
- 尊重 `rag_exclude: true` 标记
- 输出结构化的 JSON 索引文件

用法：

```bash
python3 rebuild-vault-index.py \
  --vault ~/Documents/MyVault \
  --output ~/.hermes/vault-index.json \
  --scan wiki/projects wiki/career hermes-knowledge/
```

### 配置项

- **排除目录**：`EXCLUDE_DIRS = {"archived", "archive", "log", "modes", "tracking", "task-"}`
- **排除文件名模式**（正则）：`r"^plan-"`, `r"-progress$"`, `r"-data-\d{4}-\d{2}$"`
- **始终跳过的文件**：`SKIP_FILES = {"index.md", "README.md"}`
- **默认文件新鲜度截止天数**：`DEFAULT_CUTOFF_DAYS = 90`

---

## 索引 Schema

`vault-index.json` 中每条记录的结构如下：

```json
{
  "_content_hash": "sha256哈希值",
  "path": "笔记在Vault中的相对路径",
  "title": "笔记标题",
  "summary": "摘要要点（来自summary_points或首段文本）",
  "updated": "最后更新日期",
  "author": "作者",
  "aliases": ["别名1", "别名2", "别名3"],
  "last_action": "最近一次操作描述",
  "status": "笔记状态"
}
```

**字段说明**：

- `_content_hash`：SHA256 哈希，用于增量比对判断文件是否变更
- `path`：相对于 Vault 根目录的路径，Agent 通过此路径读取原文
- `aliases`：别名列表，自动上下文注入时用于模糊匹配
- `summary`：从 `summary_points` frontmatter 字段提取，若不存在则取首段纯文本
- `last_action`：从 frontmatter 中提取，记录笔记最近一次操作
- `status`：笔记当前状态（如 draft, active, archived 等）

---

## 自动注入规则

实现 ORP 的 Agent 必须遵守以下规则：

1. **非平凡问题检测**：仅在判断用户问题需要背景知识时才触发注入，避免对简单问候或常识性问题做无意义查询。
2. **索引损坏处理**：如果 `vault-index.json` 无法解析或为空，Agent 应提示用户运行 `rebuild-vault-index.py` 重建索引，而非静默失败。
3. **索引过期检测**：若索引中的 `_built_at` 时间戳距今超过配置的刷新阈值，Agent 应提醒用户重建索引。
4. **最小注入原则**：只注入与当前问题直接相关的笔记文件，避免一次性注入全量上下文导致信息过载。
5. **读取后验证**：注入前检查文件路径是否仍然存在，防止索引指向已删除的文件。

---

## 状态

ORP v1.0 规范已**稳定**，目前运行在作者个人的 Hermes Agent 日常流程中：

- 索引 30+ Vault 条目
- 两个 Agent 共享同一 Vault
- 每日 cron 自动重建索引

---

## License

MIT License — 自由使用、修改和分发。

---

## 关注作者

**Vincent Wen** — Crypto/Web3 营销人，正在寻找 Crypto + AI 方向的职位。

- **Twitter/X**：[@VincentWen](https://x.com/)
- **GitHub**：[wjameswen888](https://github.com/wjameswen888)

如果你也在探索 AI Agent + 个人知识库的结合，欢迎来聊！

---

*Built with pain points, not product specs.*