# 字体资源目录

此目录保留[字体许可证](LICENSE-NotoSansCJK.txt)与字体资源说明。两份 Noto Sans CJK OTF 文件不纳入插件仓库，运行时落盘到 AstrBot 数据目录下的 `plugin_data/astrbot_plugin_media_parser/font/`。AstrBot 更新插件会替换整个插件目录，字体放在插件数据目录可在更新后继续使用，无需重新下载；脱离 AstrBot 本地调试时回退到此目录。

## 字体选择

仅启用文本图片渲染时准备字体，先真实加载自定义字体、配置字体族对应的本地字体和已有默认字体。缺少专用粗体时可沿用同字体族的常规字体；本地字体有效时无需联网补全默认字体，离线也能渲染。

可通过 `ASTRBOT_MEDIA_PARSER_FONT` 环境变量指定优先使用的字体文件路径。未指定或该文件不可用时，按配置的字体族及可用字体回退。

## 默认资源补全

本地候选均不可用时，从 [drdon1234/fonts 的 v1.0.0 Release](https://github.com/drdon1234/fonts/releases/tag/v1.0.0) 自动补全默认字体。补全流程校验文件大小与 SHA256；同一进程中资源就绪后快速检查文件是否存在且大小正确，异常时重新完整校验。

运行时文件名固定为：

- `NotoSansCJKsc-Regular.otf`
- `NotoSansCJKsc-Bold.otf`

下载使用唯一 `.part` 临时文件，校验通过后原子替换目标文件；失败或取消产生的临时文件会被清理。

字体自动补全需要字体目录可写并能访问 GitHub；无法直连时，可在填写代理地址后开启 `proxy.font`（字体资源下载使用代理）。补全失败时插件仍可加载，文本图片渲染会回退为原文本。
