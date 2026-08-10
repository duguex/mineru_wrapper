# CONTEXT — mineru_wrapper (PDF → 结构化 Markdown 解析)

术语表, 无实现细节。实现与操作见 `CLAUDE.md` 与 `mineru_wrapper.md`。

- **parse run** — 一次把 PDF(集合)转换为结构化 Markdown 产物的执行。
- **derived name** — 由输入 PDF 文件名经 `derive_name` 得到的稳定短键; 决定该输入的产物目录名。
- **skip key** — `parsed/<name>/paper.md` 的存在性; 存在即视为该输入已解析, 默认跳过该输入。
- **image-map** — 提取图像 hash → 文档图表标签(如 `a1b2c3d4.jpg → FIG. 1(a)`)的映射文件 `image-map.txt`。
- **finalize** — 把一次 parse run 的原始产物整理为标准 `parsed/<name>/{paper.md, images/, image-map.txt}` 子树的步骤; 由 `finalize.py` 实现(重命名/移动、image-map 进程内生成、orphan 过滤、清除 minerU 辅助文件、幂等)。raw 到达(auto/)与已最终到达(批处理/HTTP 直写 `parsed/<name>/`)两种形态共用同一入口。
- **manifest** — 每个 parse run 写入的逐输入状态汇总(`parsed/manifest.json`)。
- **output root** — parse run 的基础输出目录; 每个输入在其下产出 `parsed/<name>/…` 子树。本链路约定 output root = 输入 PDF 所在目录。
- **status** — 单个输入在 manifest 中的结果: `parsed` / `failed` / `skipped`。