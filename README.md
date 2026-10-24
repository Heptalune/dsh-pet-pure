# dsh-pet-pure · 鲸鱼娘纯粹桌宠

<p align="center">
  <img alt="平台" src="https://img.shields.io/badge/平台-Windows-8A2BE2">
  <img alt="体积" src="https://img.shields.io/badge/安装包-125MB-brightgreen">
  <img alt="动画" src="https://img.shields.io/badge/动画-106段全量-ff69b4">
  <img alt="联网" src="https://img.shields.io/badge/联网-完全离线-blue">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-orange">
</p>

一只不联网、零配置、开箱即养的桌面宠物。

她叫**鲸鱼娘**——把 DeepSeek 鲸鱼娘"深深"的全部 106 段动画搬到你的桌面上：
闲了发呆擦桌、饿了吃大闸蟹、过年写福字放烟花、上班搬砖叹气、钱包空了数钱袋皱眉。
没有账号、没有 API Key、没有弹窗更新，双击安装，她就在屏幕右下角安家了。

---

## 她会做什么

**日常陪伴**

- 106 段动画自动轮播，长时间不理她也会自己找乐子
- 待机、遛弯、打哈欠、伸懒腰、照镜子、晨间刷牙……生活感拉满
- 偶尔冒出一条自言自语气泡（台词可在设置里自定义）

**互动**

- **点她一下**：开心跃动、害羞惊讶、傲娇生气——每次反应随机
- **拖拽甩宠**：把她甩出去，会有抛物线、重力和落地弹跳
- **多开**：右键"生小肥鱼"，养一群；互相碰撞会挤来挤去
- **点击音效**：轻量反馈音，可换音效包或自定义音频
- **边缘探头**：拖到屏幕边缘，她会只探出个脑袋偷偷看你

**动作库（右键 → 播放动画）**

| 分类 | 段数 | 都有啥 |
|---|---|---|
| 闲来无事 | 6 | 呼吸休闲、东张西望、深度思考、发呆、冲屏幕说话、擦桌 |
| 遛弯 | 3 | 螃蟹走路、漂浮踏步、左转奔跑 |
| 点她一下 | 5 | 傲娇生气、元气挥手、害羞惊讶、开心跃动、挠痒咯咯笑 |
| 搬砖日常 | 6 | 思考冒泡、忙碌点按、清点归档、踱步张望、雀跃庆祝、垂头叹气 |
| 钱包状态 | 6 | 从钱袋满溢到分文不剩，六档钱包心情 |
| 逢年过节 | 14 | 堆雪人、放烟花、写福字、收红包、穿针乞巧、舞狮头、装点圣诞树…… |
| 吃货时间 | 22 | 三餐、涮火锅、大闸蟹、糖葫芦，还有月饼汤圆粽子青团等时令美食 |
| 玩耍 | 20 | 放风筝、荡秋千、骑木马、下五子棋、撸猫、抽陀螺、蓝鲸现世…… |
| 露一手 | 7 | 小提琴、吹笛子、女仆舞、宅舞、摇摆舞、扑克魔术、变鸽子 |
| 日常小事 | 16 | 写代码、哼歌、打瞌睡被惊醒、整体换装试色…… |

**窗口行为**

- 置顶常驻、全屏应用时自动隐藏、鼠标穿透模式
- 开机自启、多显示器、缩放三档（0.5x / 0.72x / 0.85x / 1.0x）
- 播放速率 1.0x–2.0x 可调

## 下载安装

到 [Releases](https://github.com/Heptalune/dsh-pet-pure/releases) 下载：

| 文件 | 体积 | 适合 |
|---|---|---|
| `dsh-pet-pure-light-setup.exe` | **125 MB** | 推荐。大多数人的选择 |
| `dsh-pet-pure-setup.exe` | 258 MB | 追求开箱最佳动画画质 |
| `*-portable.zip` | — | 免安装，解压双击 `dsh-pet-pure.exe` 即用 |

安装不需要管理员权限。两个版本**功能完全相同**，区别只在动画画质：

- **轻量版**：动画先以 WebM 播放（半透明边缘有轻微白晕），程序在后台自动逐段升级为无损画质——待机动画首次启动就转好，之后每次开机约升级一段
- **标准版**：最常播放的 11 段（闲来无事/遛弯/点她一下等，约占 95% 播放时长）出厂即无损画质，开箱零白晕

## 快速上手

1. **右键她**：一切入口都在这里——播放动画、切换大小、生小肥鱼、设置、关闭此鱼
2. **拖她**：按住甩出去试试
3. **托盘图标**：显示/隐藏、设置、退出
4. **设置 → 外观**：菜单配色、彩蛋入口（第一行那个圆头像，点开有惊喜配图）
5. **设置 → 菜单 → 菜单编排**：右键菜单里显示什么、什么顺序，全由你拖拽决定

## 自定义角色

支持热加载自定义角色：把透明 WebM/GIF 动画按分类放进
`characters/<角色ID>/videos/{idle,turn,move,click,drag,random}/`，
重新打开右键菜单即可看到新角色、点击切换，无需重启。
角色清单 `manifest.json` 可声明显示名、主题色、身体框等。

## 从源码构建

```powershell
# 依赖只要 4 个运行库 + 打包器（requirements-pure.txt）
powershell -ExecutionPolicy Bypass -File scripts\build_pure.ps1
# 可选：-SkipHotFrameseq 再省 137 MB（全部动画走运行时自动升级）
```

产物：`dist-onedir\dsh-pet-pure\`（onedir）+ 便携 zip。
安装器用 [Inno Setup 6](https://jrsoftware.org/isinfo.php) 编译 `packaging\dsh-pet-pure.iss`。

变体细节（砍了什么、怎么砍的、体积账）见 [docs/PURE-VARIANT.md](docs/PURE-VARIANT.md)。

## 已知限制

- 不含在线更新：升级 = 重新下载/重新构建
- Windows 平台优先适配；macOS / Linux 未在本 Fork 发布
- 动画素材版权归上游所有，本项目仅供个人使用与学习

## 致谢

本项目站在两位作者的肩膀上，衷心感谢他们的开源工作：

- **[PC2005-cloud](https://github.com/PC2005-cloud)** — [dsh-pet](https://github.com/PC2005-cloud/dsh-pet)：
  鲸鱼娘桌宠的创造者，全部动画素材与交互设计的来源
- **[MerZlin](https://github.com/MerZlin)** — [dsh-pet-indesktop](https://github.com/MerZlin/dsh-pet-indesktop)：
  把浏览器插件做成独立桌面应用（Python + PySide6），本 Fork 的直接上游

## 许可证

[MIT](LICENSE) ｜ 第三方素材声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)
