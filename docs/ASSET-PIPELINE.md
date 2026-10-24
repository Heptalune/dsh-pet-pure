# 动画素材流水线（ASSET-PIPELINE）

> **用途**：从零制作/替换桌宠动画素材（绿幕源视频 → 透明 WebM）、生成 GIF 变体、把新角色入库。
> **来源**：本文承接自 `README.md` 的「素材生成教学（从零制作动画素材）」「重新生成 GIF」「新增角色」三节
> （2026-10-15 下沉，正文逐字保留，只补了首尾的入口与互链）。
> **互链**：[`../README.md`](../README.md)「动画素材与自定义角色」（用户侧：换角色、热加载）、
> [`ONEDIR_PACKAGING.md`](ONEDIR_PACKAGING.md)（GIF 变体怎么进包）、[`INDEX.md`](INDEX.md)（全文档入口）。
> **素材版权**：动画素材源自参考项目 [PC2005-cloud/dsh-pet](https://github.com/PC2005-cloud/dsh-pet)，
> 授权与署名见 [`../THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md) 与 [`../LICENSE`](../LICENSE)；
> 新增/替换素材时请同步维护这两处声明。

---

## 一、素材在仓库里的位置与约定

```text
assets/
└── characters/
    └── shenshen/
        ├── manifest.json
        └── videos/
            ├── idle/
            ├── turn/
            ├── move/
            ├── click/
            ├── drag/
            └── random/
```

- `assets/characters` 是 WebM 动画源目录，包含 106 个 WebM 动画。
- GIF 目录（`assets/characters_gif`）仅在构建 GIF 变体时生成。
- 没有稳定静态头像时，不强制从 WebM/GIF 截取首帧，以避免启动变慢和打包兼容性问题。
- `assets/characters/<id>/videos/` 加它的 manifest 视为**一个角色包**：相对路径与大小写都要保持
  （打包平台之间有差异）；manifest 可选声明 `body_box`（源像素 `[x1, y1, x2, y2]`，镜像对称化），
  它是摆放/贴边钳制用的身体框（`pet/catalog.py character_body_box`），测量时按 alpha≥128 对齐
  mask 边缘，省略则退回整画布行为。这些约定与 `AGENTS.md` 的 "Context pointers" 一致，改素材前请两份都读。

---

## 二、素材生成教学（从零制作动画素材）

本项目的动画素材沿用参考项目 [PC2005-cloud/dsh-pet](https://github.com/PC2005-cloud/dsh-pet) 的**三件套流程**：`① 提示词（配方）→ ② 素材生成链（引擎）→ ③ 插件（成品）`。任何人 clone 参考仓库都可以从零生成自己的桌宠素材，本教学按该流程说明。

### ① 提示词 → 源视频（绿幕规范）

用 AI 视频生成工具（如**可灵、Runway、豆包**等；参考项目素材即由豆包生成），按提示词配方为每个动作生成一段 **10 秒绿幕视频**。配方硬性规范（参考项目的 `prompts/桌面宠物 10 秒动作提示词.md`；本项目自定义角色的配方按惯例放本地工作目录 `assets/prompt/<角色ID>/图像生成提示词.md`——该目录**不入仓库**，本文附带下面这套规范就是为了没有它也能照抄）：

- **画面**：16:9；背景纯绿幕色 `#00FF00`，无阴影、杂物、渐变或边框。
- **人物定位固定**：头顶约画幅垂直 20%（按角色头饰可调整为 15%），脚底约 85%；左右边缘约 25% / 75%；不同视频间人物大小、位置、比例完全一致。
- **安全缓冲**：头顶距顶边 ≥15%、脚底距底边 ≥10%、两侧距边缘 ≥10%；任何身体部位/道具/特效不得出画或贴边。
- **禁止平移**：双脚落点恒为画面正中，仅允许原地轴心旋转、原地跳跃；道具与角色组合视觉重心始终居中。
- **首尾帧闭环**：第一帧 = 干净的标准正面站立；第 10 秒结束必须恢复到与第一帧完全一致。
- **道具"无→有→无"**：道具/特效由角色从虚到实渐进生成、结束前由实到虚消散，不得凭空出现或残留。
- **按秒分解**：每个动作的配方按 0–3 / 3–7 / 7–10 秒（或 0–2 / 2–5 / 5–7 / 7–10）分段描述动作节奏，确保生成结果可复现。

一个动作一段视频，按动作名各存一个 mp4（如 `video/吃白饭.mp4`）。

### ② 源视频 → 透明动画（素材生成链）

参考项目 `scripts/` 提供完整 Python 素材链（依赖：Python 3 + ffmpeg + numpy + scipy），四步：

```sh
cd scripts
# step01：水印遮罩填充（源视频若带水印/角标则先填补为纯绿幕）
python watermark_step01.py
# step02：绿幕抠像 → 透明视频（两条路线二选一）
#   路线 A（默认自动化）：HSV 色相抠像，人人可复现
python chroma_step02.py
#   路线 B（精细手工，推荐）：PR 手工抠像导出带 alpha 的透明 .mov，
#   放入 pr/（文件名与动作一致，如 吃白饭.mov）后导入
python pr_import_step02.py
# step03：归一化 2160×1215 统一站立居中（对齐脚底锚点）
python normalize_step03.py
# step04：转码 640×360 透明播放变体（VP9 alpha 透明 WebM）
python encode_thumbs.py
```

> 参考项目全部 106 个动作均采用**路线 B（PR 手工抠像）**：对含第三方物品/透明边缘复杂的动作，自动 HSV 抠像易残边或误抠；`chroma_step02.py` 保留为自动化兜底。中间产物 step01~04 由脚本生成、不入仓库；`video/` 源视频与 `scripts/` 是成果、入库维护。

### ③ 透明动画 → 接入本项目

1. 把 step04 产出的 **640×360 透明 WebM** 按分类放入角色目录：

   ```text
   assets/characters/<角色ID>/videos/
   ├── idle/     待机（可多个）
   ├── turn/     转向
   ├── move/     移动
   ├── click/    点击回应
   ├── drag/     拖拽（可选）
   └── random/   随机动作池
   ```

2. 保持几何约定与播放器一致：画布 **640×360**、24fps、**VP9 alpha 透明**；角色脚底对齐画布 y=330（`catalog.py` 中 `FEET_Y=330`、落地偏移 `PAD=30`），这样桌宠窗口的脚底落地对齐才准确。
3. 命名保持稳定、避免重复；可参考 `assets/characters/shenshen/videos/` 现有 106 段动画的组织方式。
4. 如需 GIF 变体，运行 `python scripts/convert_to_gif.py --force --clean` 同步生成。

> 不想重新打包？把做好的透明 WebM 按「切换角色」的外部角色目录结构直接放入 `characters/<角色ID>/videos/`，右键菜单即可热加载新角色（用户侧步骤见 [`../README.md`](../README.md) 的「热加载新角色：文件怎么放」）。
>
> 快速验证：`python -m pytest -q` 会检查 WebM/GIF 相对路径一一对应；源码运行 `python -m pet` 或重新打包后检查对应分类是否正常播放。

---

## 三、重新生成 GIF（仅构建 GIF 变体时需要）

更新 WebM 素材后，在项目根目录执行：

```powershell
python scripts/convert_to_gif.py --force --clean
```

其中：

- `--force`：覆盖已有 GIF。
- `--clean`：删除目标目录中已经不存在对应 WebM 的旧 GIF，防止两套素材残留不一致。

转换前请确认 `imageio-ffmpeg` 已安装。生成后可以用下面的命令检查数量：

```powershell
(Get-ChildItem assets/characters -Recurse -Filter *.webm).Count
(Get-ChildItem assets/characters_gif -Recurse -Filter *.gif).Count
```

两者应当相同；还应检查相对路径是否一一对应。GIF 变体自 v4.0.0 起不再随默认发布，如何进包见
[`ONEDIR_PACKAGING.md`](ONEDIR_PACKAGING.md)。

---

## 四、新增角色（入库流程）

1. 在 `assets/characters/<character_id>/videos/` 下按动画类别建立目录。
2. 放入透明 WebM 文件（制作方法见「素材生成教学」），命名保持稳定、避免重复。
3. 如有角色身份信息，在 `<character_id>/manifest.json` 中填写名称、prompt、主题色和动作映射。
4. 如需 GIF 变体，运行 GIF 转换脚本同步生成 GIF。
5. 使用源码运行或重新打包验证角色切换、播放、气泡定位和 Chat 身份区。
6. 新增/替换素材后同步维护 [`../THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md) 的素材授权声明
   （含来源出处），这是硬要求——第三方素材的署名不能只活在 PR 里。
