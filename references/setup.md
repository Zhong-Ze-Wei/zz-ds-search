# 从没有 API Key 到可用搜索

## 对用户怎么说

建议这样说：

> 我会打开 DeepSeek 官方 API Keys 页面。请登录后创建一个密钥，名称填 zz-ds-search，然后粘贴到本机终端的隐藏输入框。不要把密钥发到聊天。

## 步骤

1. 打开官方创建入口：https://platform.deepseek.com/api_keys
2. 未登录的话由用户自己登录或注册、处理验证码。页面文案可能变化，以实际的 API keys 创建入口为准
3. 创建 Key 后按平台提示处理只显示一次的密钥。不要截图或录制
4. 在用户自己的终端运行：

~~~bash
python3 <skill_dir>/scripts/zz_ds.py setup
~~~

- 无图形界面加 --no-browser
- 只想看步骤不配置：--guide-only
- 用户已自己配置过、只想验证：doctor

5. setup 成功后自动执行一次真实 Search 验证。只有 search_verified 为 true 才算配好了。

## 成功边界

| 状态 | 可以说什么 | 下一步 |
|---|---|---|
| status 成功 | 检测到配置 | 尚未验证，执行 doctor |
| setup 后 401 | 文件已保存但密钥不可用 | 重新复制或创建；检查环境变量是否覆盖 |
| setup 后 402 | 余额不足 | 用户在平台查看余额并自行决定充值 |
| doctor 200 但空结果 | 接口有响应，未验证有效结果 | 换实际主题再查一次，不要反复重置配置 |
| doctor search_verified 为 true | 搜索实测成功 | 继续原查询；Fetch 单独验证 |

## 凭据优先级

1. 环境变量 DEEPSEEK_API_KEY
2. XDG_CONFIG_HOME 下的 zz-ds-search/credentials.json，默认是 ~/.config/zz-ds-search/credentials.json

环境变量优先于文件。setup 会验证刚保存的 Key，但后续查询仍优先用环境变量；若环境变量过期，要在真正运行 agent 的进程里更新或移除。

## 存储与安全

| 项目 | 约定 |
|---|---|
| 凭据目录 | ~/.config/zz-ds-search/，POSIX 700 |
| 凭据文件 | credentials.json，POSIX 600，原子写入 |
| 拒绝符号链接 | 文件或目录是软链直接报 unsafe_config |
| 用量状态 | ~/.local/state/zz-ds-search/usage.json，与凭据分开 |

密钥是权限保护的明文，不是加密保险箱。不写入 shell 历史、skill、README、截图、报告。

删除本地文件只能删本地副本；需要失效时由用户在官方平台撤销。

## 从 qiaomu-ds-search 迁移

如果检测到旧 skill 的凭据文件（~/.config/qiaomu-ds-search/credentials.json）且新版尚未配置，脚本会自动读取并复制到新位置，credential_source 会返回 migrated_from_qiaomu。

迁移后旧文件可以手工删除，不影响使用。

## 常见问题

| 情况 | 处理 |
|---|---|
| missing_api_key | 运行 setup |
| interactive_required | 换到用户自己的可见终端；不要在管道或 agent 伪终端里输入 |
| unsafe_config | POSIX 文件权限应为 600；不支持软链 |
| invalid_key | 复制时带了空格或换行，重新复制 |
