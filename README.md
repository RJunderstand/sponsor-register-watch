# sponsor-register-watch

UK Home Office《Register of licensed sponsors: Workers》的自动镜像。
解决的问题：定时求职扫描任务的 outputs 每次运行被清空，gate 1a（sponsor 牌照核查）没有可 grep 的本地副本；且云端环境无法直接下载 gov.uk 文件（403），`web_fetch` 又在约 1,419 行截断（全表约 14 万行）。

## 工作原理

- GitHub Action 每周一 06:30 UTC 自动从 [GOV.UK 发布页](https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers) 抓取最新 CSV，存为固定文件名 `register.csv`（约 10.9 MB，约 142,000 行）。
- `SNAPSHOT_NAME.txt` 记录官方文件名（含快照日期），`LAST_UPDATED.txt` 记录抓取日期。
- 行数低于 10 万会拒绝提交（防止抓到截断/改版页面覆盖好数据）。
- 定时任务每次运行时 `git clone --depth 1` 本仓库，然后本地 grep，1 秒出结果。

## 一次性设置（约 5 分钟）

1. 登录 github.com（账号 RJunderstand），新建 **public** 仓库，名字：`sponsor-register-watch`。
   - Public 的原因：这是公开政府数据，public 仓库让定时任务无需任何 token 就能 clone。
   - 不要勾选 "Add a README"（保持空仓库，直接上传文件）。
2. 在仓库页面点 **uploading an existing file**，把本压缩包里的所有文件拖进去（注意 `.github/workflows/update-register.yml` 的目录结构要保留 — 用 GitHub 网页上传文件夹时直接把解压后的整个内容拖入即可）。Commit。
   - 如果网页上传丢失了 `.github` 目录：在仓库里点 Add file → Create new file，文件名输入 `.github/workflows/update-register.yml`（输入 `/` 会自动建目录），把 yml 内容贴进去。
3. 打开仓库的 **Actions** 标签页 → 如提示则点 "I understand my workflows, go ahead and enable them" → 选 "Update sponsor register" → **Run workflow** 手动跑第一次。
4. 约 1 分钟后仓库里应出现 `register.csv`。完成。以后每周一自动更新，不用再管。

## 定时任务运行时怎么用（写进任务 prompt）

```bash
git clone --depth 1 https://github.com/RJunderstand/sponsor-register-watch.git /tmp/sponsor-register
cat /tmp/sponsor-register/LAST_UPDATED.txt   # 快照超过 14 天要在 digest 里标注
grep -i "Employer Name" /tmp/sponsor-register/register.csv
# 或: /tmp/sponsor-register/check_sponsor.sh "Employer Name"
```

列结构：`Organisation Name, Town/City, County, Type & Rating, Route`（注意列名是 `Type & Rating` 不是 `Rating`，值形如 `Worker (A rating)`）。

两个必须记住的陷阱（来自 2026-08-23 实战）：

1. **子公司单独查。** 大学/学院常用 `Support Services Ltd` / `Enterprises Ltd` 雇人，母公司的牌照不覆盖子公司（BCU、Nescot 都踩过）。
2. **宽松 grep 出假阳性。** 看完整公司名再下结论（搜 Pearson 会出 Dan Pearson Studio）。

## 备份路径（笔记本文件夹）

`register.csv` 同时手动放一份在笔记本已连接文件夹 `sponsor-watch/` 里。GitHub 不可用时定时任务退回用 `device_bash` grep 那份（需笔记本在线）。以 GitHub 版为准，笔记本版仅作备份，不必每周同步。
