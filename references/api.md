# DeepSeek Search / Fetch 契约

核对日期：2026-10-19。已用 open 直连实测官方文档原文校对，以下参数与官方页一致。原始来源：[指南](https://api-docs.deepseek.com/guides/search/)、[Search 参考](https://api-docs.deepseek.com/api/search)、[Fetch 参考](https://api-docs.deepseek.com/api/fetch)。

## 请求

- 地址固定：https://api.deepseek.com/v1/tools/search 或 /fetch
- 鉴权：Authorization Bearer Key，JSON POST
- 不含 model 参数，不是聊天补全
- 本脚本使用自定义 no-redirect handler，绝不把 Bearer 转发到重定向目标

## Search 参数

| 字段 | 约束 |
|---|---|
| query | 必填，1–128 Unicode 字符，不能全空白 |
| count | 整数 1–50，API 默认 10，本工具默认 5 |
| need_content | 默认 false；true 也可能没有正文 |
| ori_question | 可选原问题，1–128 字符 |
| localization | 可选 IP 地址；不自动采集用户 IP |

### 支持的运算符（原样保留）

- site:example.com
- site:a.com OR site:b.com
- -site:example.com
- -advertisement
- -"release notes"
- "exact phrase"
- lang:zh,en
- after:2026-01-01 / before:2026

过滤日期不是发布时间准确性的保证。

## Search 返回

results 数组（可以为空），每项可能包含：

title / url / snippet / summary / content / site_name / site_icon / date / authority / rerank_score

- date 可为 null，不伪造日期
- rerank_score 0–3；官方视 1.5 以上为较高质量
- authority 为 -1（未知）或 0–4；3 以上为较高权威档位

两者都不是事实核实结论。

## Fetch 参数与返回

请求只有 url：HTTP/HTTPS、含主机、不含用户名密码、最长 8192 字符。

返回 title（可空字符串）加 content（Markdown 正文）。

HTTP 200 且正文非空，可能只是首页/导航页。

- 403 是安全拒绝，换公开来源
- 404 可能是不存在、站点策略、内容类型不支持或无法提取，不等价于网站不存在

## 重试与错误

只对 408、429、5xx 和网络失败重试：

- 每次默认 30 秒超时，最多 3 次尝试
- Retry-After 为数值且大于 10 秒时直接停止
- 4xx（除 408/429）永不重试
- 响应超过 16 MiB 报 response_too_large
- schema 不符报 invalid_response，不静默当空

| HTTP | code | 含义 | 处理 |
|---|---|---|---|
| 400 | http_error | 参数无效 | 核对 query/count/URL |
| 401 | http_error | Key 无效或撤销 | 重新 setup |
| 402 | http_error | 余额不足 | 用户自行在平台处理 |
| 403 | http_error | Fetch 被安全拒绝 | 换来源 |
| 404 | http_error | 无可用正文 | 换来源或改用 open |
| 408 | http_error | 读取请求体超时 | 可重试 |
| 429 | http_error | QPS 超限 | 已自动重试 2 次 |
| 500/504 | http_error | 服务端/下游超时 | 已自动重试 2 次 |
| — | missing_api_key | 未配置 | exit 2 |
| — | network_error | 网络/TLS | 检查连接 |

## 计费与限流（会变化）

文档在核对日列出的 Search 和 Fetch 标价均为每 1,000 次成功请求 0.75 美元；count 不改变单次价格。

- 成功且非空才计费；空搜索、无正文、失败、超时不计费
- 每账户 Search 100 QPS、Fetch 25 QPS，两者独立计算；Fetch 另受目标域名限流
- QPS 按账户计算，与用哪个 Key 无关；多个进程仍共享账户限额

关于免费的说明：官方文档提到，经 DeepSeek Harness / Claude Code / Codex 使用 DeepSeek 服务端工具的调用当前免费，2026-11-01 北京时间 00:00 起计费。本脚本是直接 REST 调用，不据此承诺享受免费。

估算：3 次搜索加 3 次抓取按标价约 0.0045 美元（仅估算，实际以平台账单为准）。

## 直连兜底 open（本项目新增）

当需要在已抓取的文档里跳转到外部链接，又不想为一次短抓取付费时用 open：

- 只支持 text/html 和 text/plain
- 不执行 JS，不携带任何鉴权
- 16 MiB 硬上限
- 返回 route 为 direct_fetch，并带 evidence_note 提醒措辞需回原文确认
- 它不是 DeepSeek Fetch 的等价替代，引用时要区分路线
