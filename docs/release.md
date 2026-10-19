# 发布检查表

发版前逐项确认，每项都要有可验证的结果，不能只打勾。

## 1. 代码

- [ ] python3 -m unittest discover -s tests 全绿
- [ ] python3 -m py_compile scripts/zz_ds.py scripts/quality.py 无输出
- [ ] python3 scripts/zz_ds.py --help 列出 9 个子命令
- [ ] 无 Key 时 status 返回 exit 2 且提示官方链接

## 2. 安全

- [ ] 全仓搜索无真实 Key（sk- 开头的长串）
- [ ] 测试代码里的 Key 都是明显假值
- [ ] 凭据文件不出现在仓库里
- [ ] .gitignore 覆盖 .env、credentials.json、__pycache__

## 3. 真实链路

- [ ] doctor 返回 search_verified 为 true
- [ ] search 能返回真实结果且 quality.kept_count 大于 0
- [ ] open 能抓到真实页面且 title 无残留 HTML 实体
- [ ] 无效 Key 返回 401，错误信息不含 Key 本身

## 4. 文档

- [ ] SKILL.md 不超过 120 行
- [ ] references 四篇链接都能打开
- [ ] README 的安装命令指向真实仓库地址
- [ ] NOTICE 保留上游版权声明

## 5. 分发包

- [ ] npx skills add 能正确安装并识别 SKILL.md
- [ ] 在新机器（无既有配置）上从 setup 走通
- [ ] 从 qiaomu-ds-search 迁移场景实测一次
