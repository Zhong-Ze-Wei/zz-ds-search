# zz-ds-search

用 DeepSeek 官方搜索接口做联网检索：搜索、抓网页正文、补充来源、核实事实。

**分工**：调用这个 skill 的模型负责理解问题、拆查询、判断相关性；脚本只负责调接口、去重、抓取和记次数。不调用任何聊天模型，不建向量库，零第三方依赖。

## 什么时候用

| 该用 | 不该用 |
|---|---|
| “搜一下最近的模型动态” | “翻译这段话” |
| “再找几个来源核实这个结论” | “在本地文件里找一下” |
| “抓一下这个网页，看看原文怎么说” | “帮我绕过登录看付费文章” |
| “用 DeepSeek 查这个报错的官方解法” | “把这个网站整站爬下来” |

## 安装

~~~bash
npx skills add Zhong-Ze-Wei/zz-ds-search
~~~

## 配置（没有 Key 也能开始）

让助手带你配置，或者自己在本机终端跑：

~~~bash
python3 scripts/zz_ds.py setup
~~~

它会打开 DeepSeek 官方 API Keys 页面，说明步骤，然后在隐藏输入框里读取密钥，保存后执行一次真实搜索验证。

也可以在终端直接设置环境变量 DEEPSEEK_API_KEY，它优先于配置文件。

**前置条件**

- Python 3.9 以上，只用标准库
- 一个 DeepSeek API 账号和可用额度（聊天会员不等于 API 额度）

## 命令

~~~bash
python3 scripts/zz_ds.py search  "短查询" --count 5
python3 scripts/zz_ds.py research "原始查询" --query "展开问题1" --query "展开问题2"
python3 scripts/zz_ds.py fetch   "https://api-docs.deepseek.com/guides/search/"
python3 scripts/zz_ds.py open    "https://example.com/article"   # 直连兜底
python3 scripts/zz_ds.py merge   "原始问题" round1.json round2.json
python3 scripts/zz_ds.py status | doctor | reset
~~~

| 命令 | 作用 | 计费 |
|---|---|---|
| search | 单次搜索，自动去重 | 是 |
| research | 执行模型给的查询计划 | 是 |
| fetch | 抓 URL 的 Markdown 正文 | 是 |
| open | 直连抓公开网页（兜底） | 否 |
| merge | 离线合并多批结果 | 否 |
| status | 只看本地配置 | 否 |
| doctor | 一次真实搜索验证 | 是 |
| setup | 配置密钥 | 可能 |

## 和原版 qiaomu-ds-search 的差别

本项目基于 [qiaomu-ds-search](https://github.com/joeseesun/qiaomu-ds-search)（MIT）重构，保留它的核心判断，去掉不适用于分发的负担。

| 上游做法 | 本项目做法 | 原因 |
|---|---|---|
| 预算靠模型自己记，脚本不跨进程记账 | 脚本跨调用累计，输出 used 和 advisory，但不拦截 | 默认入口如果硬拦，体验代价大于多花的几毛钱 |
| 只经 DeepSeek Fetch 抓正文 | 增加 open 直连兜底，标注 route 区分 | 文档里跳外链时不必每次都付费抓取 |
| 人物简历正则适配器（年份、婚恋、标题猎奇词） | 全部删除 | 中文耦合的历史化石，对通用研究无价值 |
| facts 和 conflicts 字段 | 删除，改为模型判断加清楚标注 | 脚本做语义判断是过度承诺 |
| SKILL.md 全塞（配置、流程、契约、费用） | SKILL.md 68 行，其余按需读 references | 每次加载都交 context 税 |
| 三套自证材料（manifest、interface、reports） | 一份 README 加 release 检查表 | 维护成本大于收益 |
| Python 3.10 以上 | Python 3.9 以上 | 实测 3.9 可跑，别再让用户白装 |

**完整拆解报告**见 [docs/analysis.md](docs/analysis.md)。

## 它不做什么

- 不保证比其他搜索引擎准
- 不保证日期准确，date 为 null 就是未知
- 不绕过登录墙、付费墙
- 不做全站爬取
- 不替用户充值

**详情**：[信任边界](references/trust.md)

## 费用

DeepSeek 官方当前标价：Search 和 Fetch 各每 1,000 次成功请求 0.75 美元。逐次累计会在每次调用后显示。

价格和限流以 [官方文档](https://api-docs.deepseek.com/guides/search/) 为准，本项目不承诺长期免费。

## 开发

~~~bash
python3 -m unittest discover -s tests -v
python3 scripts/zz_ds.py status
~~~

## 许可与致谢

MIT。基于 [向阳乔木的 qiaomu-ds-search](https://github.com/joeseesun/qiaomu-ds-search) 重构，详见 [NOTICE](NOTICE)。
