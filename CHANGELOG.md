# CHANGELOG

## 0.1.0 — 2026-11-04

初版。

- 快照：整目录压 zip，跳过 `__pycache__` / `.git` / `node_modules` / `.venv` / `snapshots`
- 列表：按插件分组，带时间戳、文件数、字节数
- 回滚：原目录改名留底，解压快照覆盖
- 开机自检：延迟 30 秒，扫 `star_manager` 的加载记录，配不上的自动回滚

踩过的坑记在这儿：

- 日志里工具参数的长行也含「Loading plugin」，宽松正则会误判。改成只认 `[star.star_manager:N]:` 前缀。
- 插件加载写在 `AstrBot started` 之前，扫日志区间要取「上一次启动 → 本次启动」，不能从 started 往后扫。
- 拍 `guard` 自己时会把已有快照也打进去，越滚越大。`snapshots` 加进跳过名单。
