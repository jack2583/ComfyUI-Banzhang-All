# custom 分支魔改与合并指南

> 合并 `upstream/main` 前先看这里。只记录本地相对上游的差异与保住它们的方法。

## 状态

- 分支：`custom`；上游：`upstream/main`
- 当前 HEAD：`f0f1f48`；已合并上游到 `729ccc2`（v2.1.3）
- 本地差异：4 个文件

## 差异文件与保留点

| 文件 | 必须保留 | 冲突处理 |
|---|---|---|
| `js/bz_project_sync.js` | `applyProjectBundle()` 里从 `clips[].prompt_seg` 收集 `legacySegs` 兜底，以及 `desk._emptyFallback` 标记 | 保留本地注释和下方 body；调用必须用上游新签名 `waitDesk(castNode, waitMs)`。写回 `waitDesk(waitMs)` 会让兜底失效 |
| `js/csglide_desk.js` | ① `presetNames` + ✎ 就地改名；② `_emptyFallback` 保留预设、`lastGen = cp.gen`、hydrate 后立即 `setSegText`；③ 全局预设（`localStorage["bz_desk_presets_v1"]` + `bz:desk-presets-change` / `storage` 事件） | 上游已用 `segEditVersion`/`edSegDirty` 替代 `deskHydrateUntil`，不要恢复旧变量；三层本地改动都要在；不要让工程库里的旧 presets 回灌全局 |
| `nodes/banzhang_video_tools.py` | output 是唯一根目录；默认 `output/<一采｜二采｜三采｜超分>/`；`🏷️ 文件前缀` 默认跟随工程名；`📂 自定义保存路径` 只能是 output 下的相对路径，绝对路径报错 | 接受上游对元数据/编码等非路径修复，再把本地路径语义套回去；不要回退成绝对路径或固定前缀 |
| `workflows/minimax h3-三采.json` | 本地配套工作流 | 只动一行，冲突以本地为准 |

## 不要回退的上游改动

- `waitDesk(castNode, timeout)` / `deskNodeOf(castNode)`：按 `prompt_text` 接线定位文案台，未接线就不灌。
- `segEditVersion` / `edSegDirty` 替代 `deskHydrateUntil`；`_hydrateFromStore` 竞态闸门。
- 段词真值在 `segment_bindings.desk_text + desk_rev`，`desk.segs` 只是投影；`emitDeskChange` 只带身份不带正文。

## 合并步骤

```bash
cd "E:/ComfyUI/ComfyUI-Installs/ComfyUI/ComfyUI/custom_nodes/ComfyUI-Banzhang-All"
git fetch upstream
git checkout custom
git merge upstream/main

git status --short
git diff --cc js/bz_project_sync.js
git diff --cc js/csglide_desk.js
git diff --cc nodes/banzhang_video_tools.py
```

解冲突时只改冲突标记附近的行，不要用 `git checkout --ours/--theirs <file>` 覆盖整文件。删干净 `<<<<<<< / ======= / >>>>>>>` 后校验：

```bash
git diff --check
tmp=$(mktemp -d)
cp js/csglide_desk.js "$tmp/csglide_desk.mjs"
cp js/bz_project_sync.js "$tmp/bz_project_sync.mjs"
node --check "$tmp/csglide_desk.mjs"
node --check "$tmp/bz_project_sync.mjs"
python -m py_compile nodes/banzhang_video_tools.py
git add -A
git commit --no-edit
```

## 验收

- 切历史工程后文案台显示该工程的词，不是空白或上一部片子的词。
- 预设增/删/改名后，同图其它文案台和其它标签页立即同步；刷新后不被工程库旧预设盖回。
- 出片默认落 `output/<阶段子目录>/`，前缀跟随工程名；手填相对路径落在 output 下，绝对路径报中文错。
- 拖入视频还原正常。

## 提交索引

`ef124d0` 配套工作流 · `ec8b50c`/`d70eebd` 出片路径与前缀 · `4442311` 预设 ✎ 改名 · `6c136bf` 历史工程词 · `70255c5` 合并上游 · `f0f1f48` 预设全局同步

局限：全局预设用 localStorage，仅同源浏览器共享；跨机器/跨浏览器尚未覆盖。
