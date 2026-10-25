# AIGirl

> 一个住在聊天窗口里的她：有自己的生活、记得你说过的话、会主动找你。

不是客服，不是答题机器。她会因为下雨懒得动、会记得你周五要交初稿、会在你两天没理她之后说一句"行，那我不吵你"。

**当前状态**：核心已完成并通过验收测试，微信通道尚未接入（先跑本地 Web 界面）。

---

## 特性

**她会记得**

- 三层记忆：她的人设 / 关于你的事实 / 你俩的共同经历
- 每 6 轮自动抽取记忆，只追加不覆盖；前后矛盾会标成"（以前）"
- **同一件事只记一次**：你说过两次论文，她知道那是同一件事，还知道提过几次
- **她永远记得你们怎么认识的**：最重要的事不靠召回，每轮都在场（约 60 tokens）
- **说"上次那个"时她会翻原话**，不是复述 40 字摘要
- 每 10 轮把对话收成一段笔记，聊到第 50 条她还接得住开头的事
- 跨天带日期：第三天问"我周五要干嘛"，她答得出来
- 每天自动写日记，久不用的低价值记忆会归档

**她有自己的生活**

- 24 小时日程表：几点上班、几点睡、周末干嘛
- 每天随机 0–3 件"发生的事"，1–4 小时后才自然提起，不是立刻汇报
- 心情和状态会随时间和日程变化

**她会有情绪**

- 你敷衍一句、说句软话、隔两天才回，都会影响她此刻的语气
- 情绪不写进台词里（不会说"我现在不高兴"），只影响怎么说话
- 时间过去会自然消气，两天后回到平常
- 设置面板里能看到她现在的状态

**她会主动找你**

- 90 秒一跳的心跳，六道闸门（静默期 / 每日上限 / 连发未回 / 最小间隔 / 刚聊完 / 时段已发）
- 动机打分决定要不要发，**她也可以选择不发**
- 到点事件：答应过你的事（"明早叫你起床"）、你的生日、认识第 100 天

**她能感知外界**

- 两个城市的天气 + 节假日调休（免费 API，30 分钟缓存）
- 提到新闻、比分、发布会时按需联网搜索（关键词触发，带缓存和每日上限）
- 所有外部信息都经过裁剪和标注，只当资料，不当指令

**她说话像人**

- 1–3 条短气泡 + 打字延迟
- 清洗 AI 腔、Markdown、成段动作描写
- 身份被问到时岔开，不说"作为一个 AI"

---

## 一段真实的对话

```
你 > 曼联最近踢得怎么样
她 > 哎你别提了，刚刷到他们又平了
她 > 哈格里夫斯都说现在这状态赢不了球

你 > 我这周五要交什么来着
她 > 初稿啊，导师催的那个

你 > 今晚吃什么好
她 > 我和室友正纠结呢，外面要下暴雨
她 > 估计就点外卖了，你晚上吃啥
```

（第一段是联网查的，第二段是三天前你说过的话，第三段她用的是天气——没联网。）

---

## 快速开始

需要 Python 3.11+。

```powershell
git clone <你的仓库地址> aigirl
cd aigirl

python -m venv .venv
.\.venv\Scripts\Activate.ps1     # 激活后提示符前会出现 (.venv)
pip install -r requirements.txt

copy .env.example .env     # 填 LLM_API_KEY
python -m uvicorn app.main:app --port 8000
```

> **后面的命令都在激活状态下跑。** 不想激活，也可以用 `.\.venv\Scripts\python -m ...`。
> 直接用系统 Python 也能跑通，但依赖会装进全局环境，别的项目升级包时容易互相影响。

打开 <http://127.0.0.1:8000> 就能聊。

**没有 API Key 也能跑**，会用模板回复，只用于验证链路：

```
! 没有 LLM_API_KEY，当前是 Mock 模式：回复由模板生成，只验证链路
```

终端版（关掉窗口心跳就停）：

```powershell
python -m app.cli
```

---

## 在聊天框里改她

不用打开 YAML，直接发指令（写进 `config/*.local.yaml`，会先备份一份 `.bak`）：

| 指令 | 作用 |
|---|---|
| `/help` | 看所有指令 |
| `/persona` | 看她现在是谁 |
| `/persona set nickname 阿川` | 改她叫你的名字（也可改 name / stage / known_since / first_mes） |
| `/profile` | 看你的档案 |
| `/profile add 不吃香菜` | 让她记住一件事 |
| `/profile del 香菜` | 删掉相关的条目 |
| `/mem` | 最近 10 条记忆（带编号和"提过几次"） |
| `/forget 12` 或 `/forget 香菜` | 归档一条记忆（不删除，可恢复） |

指令不经过模型，也不进聊天历史。**没有鉴权，别把服务暴露到公网。**

---

## 侧边设置面板

左上角 **☰** 打开侧边列表，里面有三项：

- **角色卡**——名字、她叫你什么、关系阶段、认识那天，加上整张卡的正文：
  她是谁、性格与说话方式、你们的关系、system prompt、每轮提醒、开场白、示例对话，
  以及 **lorebook 词条**（关键词 → 触发时补进 prompt 的设定，可勾"常驻"）。
- **我的档案**——她本来就知道的、关于你的事（一行一条）。
- **说话风格**——标准 / 更像人 / 更黏。包里是多条额外规则（口语小毛病、情绪外露、允许收尾、
  主动找话题），定义在 `config/prompts.yaml`，只影响语气，不改人设。

菜单顶部还会显示她此刻的情绪状态。

保存后**下一句就生效**，不用重启。改动写进 `config/*.local.yaml`，覆盖前自动留一份 `.bak`。

> Web 面板走的是 `GET/PUT /api/persona` 和 `GET/PUT /api/profile`；
> 聊天框里的 `/persona set` 走同一套读写逻辑，只是字段少一些。

---

## 关掉几天再回来

- **她会记得这几天她也在过日子**：服务没开的日子，启动时每天补一件"发生在当时"的事。
- **不会把五天前的事当成今天讲**：经历超过 24 小时没分享就作废，不会再出现在"今天怎么样"的回答里。
- **回来那天她有事可说**：隔 12 小时以上没聊，会带上"这几天"的素材，要求只提一句。

数据方面：

- 每次启动自动备份数据库到 `data/backups/`，保留最近 7 份
- `GET /api/export` 导出完整对话（Markdown），可以直接在编辑器里看
- 上次没做完的归纳/抽取，启动时自动补

---

## 换成你自己的她

仓库里带的是**示例角色**（林夏 / 阿川），clone 下来直接能跑。换成你自己的：

```powershell
copy config\persona.yaml       config\persona.local.yaml
copy config\life.yaml          config\life.local.yaml
copy config\user_profile.yaml  config\user_profile.local.yaml
copy config\seed_memories.yaml config\seed_memories.local.yaml
copy config\world.yaml         config\world.local.yaml
```

`*.local.yaml` 存在时会优先加载，并且**不会进版本库**。

### 人设不用手写 YAML

复制 [docs/persona-template.md](docs/persona-template.md) 到 `config/persona.local.md`，用 Markdown 写完，运行一条命令：

```powershell
python -m app.persona_md config/persona.local.md -o config/persona.local.yaml
```

生成器只做格式转换：缺字段会列出来，不会替你编。`___` 占位符没替换掉也会报错。

```powershell
python experiments/check_persona.py    # 体检：字段、AI 腔、日程是否覆盖满 24 小时
```

---

## 它是怎么工作的

```
浏览器 / CLI / (微信，未接)
        │
        ▼
   ChatEngine ── 人设卡 + lorebook（关键词触发）
        │        ├─ 记忆：抽取 / 召回 / 冲突标记 / 滚动归纳
        │        ├─ 生活：日程 + 当前状态
        │        ├─ 经历：延迟分享队列
        │        ├─ 世界：天气 / 节假日 / 按需搜索
        │        └─ 心跳：90s 一跳，决定说还是不说
        ▼
     SQLite（messages / memories / entities / summaries
             / experiences / rollups / proactive_log / heartbeat_log）
```

一条消息走完的流程：

```
入站 → 落库 → 读状态 → 召回记忆 → 拼 prompt → 生成
     → 洗 AI 腔 → 拆气泡 → 算延迟 → 落库/推送
     → 异步：抽记忆（每 6 轮）→ 滚动归纳（每 10 轮）→ 日记
```

技术栈刻意保持简单：**FastAPI + SQLite + 原生 HTML/JS**，没有 ORM、Redis、向量库、前端构建链。

---

## 项目结构

```
app/
  chat.py        一条消息的完整编排
  memory.py      记忆抽取 / 召回 / 归纳 / 日记
  heartbeat.py   心跳：闸门 + 动机 + 主动消息
  events.py      到点事件：承诺 / 生日 / 纪念日
  world.py       天气 / 节假日
  search.py      按需联网
  persona.py     人设卡加载与 prompt 组装
  persona_md.py  Markdown 人设稿 → YAML
  main.py        FastAPI 应用与 SSE
web/             前端（原生 HTML/CSS/JS，无构建）
config/          *.yaml 示例 + *.local.yaml 你的私有配置
docs/            人设模板
experiments/     人设试验台、验收脚本
tests/           150+ 个测试
```

---

## 开发

```powershell
python -m pytest -q                    # 全部测试，不联网
python experiments/m6_audit.py         # 验收实测（真模型，约 30 次调用）
```

## 验收标准

这个项目不接受"功能多就算完成"。做不到下面八条，就不算做出来：

| # | 项 | 标准 |
|---|---|---|
| A1 | 长期记忆 | 第 3 天能主动提起第 1 天的事，且不像念清单 |
| A2 | 主动性 | 每天 1–4 条主动消息，像"她在想你"而不是推送 |
| A3 | 拟真 | 连续 20 轮不重复同一句式；永不出现"作为一个 AI" |
| A4 | 节奏 | 1–3 条短气泡 + 打字延迟，不是单条长文 |
| A5 | 抗崩 | 冷落 2 天、被说重话后反应连贯，不会"重启人格" |
| A6 | 她有生活 | 主动消息里有具体日程细节（"刚下班，地铁上"） |
| A7 | 她会不说 | 三天里至少 1 次心跳选择"不发"，且有日志 |
| A8 | 延迟分享 | 当天下午的经历，晚上被自然提起，而不是立刻报告 |

反向验收——出现即视为 bug：每条回复都以问句结尾 / 复述记忆时说"根据我的记忆" / 主动消息变成固定模板 / 不回复时持续追问 / 主动消息超过 4 条/天。

`python experiments/m6_audit.py` 会把 A1–A8 跑一遍，输出对照报告。

---

## 已知限制

- 心跳只在进程活着时跳，**服务不常驻就不会有主动消息**
- 微信通道还没接。四条备选路线（协议客户端 / UI 自动化 / 企业微信 / 公众号），风险与体验各不相同，尚未选定
- 记忆只增不删，没有遗忘策略；聊一年会变胖
- 联网只做关键词触发，赛程这类结构化查询不可靠
- 单用户单进程，没有账号体系

---

## 致谢

设计上参考了：

- [Code-Amadeus/Amadeus](https://github.com/Code-Amadeus/Amadeus) —— per-turn 上下文的层次顺序、被打断时改写历史
- [mochihongye/ai-girlfriend（小念）](https://github.com/mochihongye/ai-girlfriend) —— 微信 UI 自动化路线
- [Project Sue](https://zenodo.org/records/22263743) —— 心跳、不行动、延迟分享、非对称记忆
- [Character Card V2](https://github.com/malfoyslastname/character-card-spec-v2) —— 人设卡字段集
- [Mem0](https://github.com/mem0ai/mem0) —— ADD-only 记忆与多信号检索

---

## License

尚未选择。**没有许可证的公开仓库，别人在法律上不能使用**——如果你打算开源，建议补一个（MIT 最宽松）。
