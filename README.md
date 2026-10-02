# AI 新闻 Skill

手动触发的中文 AI 新闻采编与本地视频制作技能。它帮助代理发现候选、核对原始来源和新增信息，再制作蓝色横屏新闻，交付视频、封面、字幕、双标题和来源发布包。

技术标识保留 **`ai-morning-news`**，节目名为“AI新闻”。这是现用技能的独立公开副本，默认 1920×1080、30fps、Qwen3-TTS / Dylan 男声、无 BGM。新闻范围接续上期采编截止；间隔达到三天或没有可靠上期时用本期前24小时。新闻数量由有效信息决定。

## 能做什么

- 指导代理检索官方公告、文档、仓库、可靠报道及公开社交讨论；对主张、时间、归因、数字基线和限定做核验。
- 校验 `episode.json`、claims、已打开的来源快照、跨期窗口和资料映射，生成事实与发音检查清单。
- 用本地 Qwen3-TTS 配音、SenseVoice 辅助转写，按实测声音安排字幕、真实资料和双层时间导航。
- 渲染 MP4、封面、SRT，检查最终音轨与关键帧；审阅通过后生成本地发布包，保留修订版本。

检索和事实判断需要代理及可用的搜索/浏览工具；脚本不会自动证明新闻真实。`pipeline.py publish` 是本地打包。平台投稿是安装者自行配置工具后的可选流程，见 [平台投稿](references/platform-publishing.md)。

## 安装

需要 Python 3.12；完整制作还需要支持本脚本 CUDA/bfloat16 推理方式的 NVIDIA GPU。公开版沿用现有 GPU 实现，没有 CPU 回退。公开包没有模型、虚拟环境、字体、参考音效、录屏、历史成品、账号或凭证。

先克隆到一个新的目录，避免覆盖已有同名技能：

```powershell
git clone https://github.com/tt1145142222/ai-news-skill.git ai-morning-news
Set-Location .\ai-morning-news
```

用于 Codex 时，将**整个仓库内容**放到你选用的技能根目录下的新 `ai-morning-news` 文件夹，保留 LICENSE 和第三方说明。常见技能根目录是用户目录中的 `.codex/skills`；使用自定义 Codex 配置时按实际根目录安装。如果目标已经存在，先保留原目录，在独立配置中试用或核对差异后更新。

安装后可用 `$ai-morning-news` 调用。也可先在克隆目录进行配置和结构演练。

## 本地配置与依赖

在技能目录执行以下 PowerShell 示例。`..\news-runtime` 是可自行修改的项目位置，缓存、模型与成品都写入该项目；配置文件会生成绝对路径并被 Git 忽略。

```powershell
$newsProject = [IO.Path]::GetFullPath((Join-Path (Get-Location) '..\news-runtime'))
New-Item -ItemType Directory -Path $newsProject -Force | Out-Null
python -B .\scripts\configure.py --project-root $newsProject
python -m venv (Join-Path $newsProject 'tools\venv')
& (Join-Path $newsProject 'tools\venv\Scripts\python.exe') -m pip install -r .\requirements.txt
python -m venv (Join-Path $newsProject 'tools\qwen-tts-venv')
```

默认本地配置为 `config/profile.local.json`，公开样例为 [assets/profile.example.json](assets/profile.example.json)。生成器只写配置，不下载依赖、不登录；已存在的配置默认拒绝覆盖。使用 `--delivery-root` 或 `--fallback-root` 可指定独立成品/备用输出盘。备用目录默认在同一项目内；需要跨盘备用时自行指定。也可用 `pipeline.py --profile 配置路径 子命令` 选择其他配置。

自行从以下上游安装资源并保留对应许可：

| 资源 | 项目相对位置与要求 |
| --- | --- |
| 主运行依赖 | `tools/venv`，依赖列在 `requirements.txt`：NumPy、Pillow、SoundFile、sherpa-onnx、imageio-ffmpeg |
| Qwen GPU 环境 | `tools/qwen-tts-venv`；按 [Qwen 官方安装说明](https://github.com/QwenLM/Qwen3-TTS#environment-setup) 安装匹配 CUDA 的 PyTorch，再在该环境安装 `requirements-voice.txt`。脚本采用 `sdpa`，没有强制 FlashAttention |
| 配音模型 | [Qwen3-TTS-12Hz-1.7B-CustomVoice](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice) 完整模型目录放到 `models/Qwen3-TTS-12Hz-1.7B-CustomVoice`，包括 `generation_config.json` 和 `speech_tokenizer/`；脚本只做离线读取 |
| 转写模型 | [sherpa-onnx SenseVoice 导出包](https://huggingface.co/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09)，解压到同名 `models/` 子目录，包含 `model.int8.onnx`、`tokens.txt` |
| 中文字体 | [Noto CJK Sans](https://github.com/notofonts/noto-cjk)，将 `NotoSansCJKsc-Regular.otf` 放到 `tools/`；默认把同一字体也用于旧版兼容字体字段，可另行配置有权使用的字体 |
| FFmpeg | 由 imageio-ffmpeg 定位；实际二进制许可证与编译选项以取得的构建为准 |

两份 requirements 是依赖清单，没有锁定已在全新环境验证的版本。CUDA、PyTorch与Qwen版本应按上游兼容要求选择。Linux/macOS 的 Python 路径生成得到 `bin/python`，完整音视频流程未在这两个系统验证；没有兼容GPU时可做采编、结构校验和清单生成。

```powershell
$newsPython = Join-Path $newsProject 'tools\venv\Scripts\python.exe'
& $newsPython .\scripts\pipeline.py doctor
```

`doctor` 会做基础依赖、部分模型文件、字体和空间检查，并创建配置所指输出/成品目录进行短暂写入测试。它不是完整预检：尚未检查 SenseVoice 模型文件、Qwen 的 `generation_config.json`、独立 Qwen 环境中的包或 CUDA 可用性；请按上表检查并通过实际制作验证。输出盘至少需要10GiB剩余空间且满足预计产物两倍空间；预计产物默认2GiB，不是模型下载空间预算。

## 输入、输出与使用

用法示例：

> 使用 $ai-morning-news 制作本期 AI 新闻，核对原始来源，交付横屏视频、封面、字幕、标题和来源发布包。

> 使用 $ai-morning-news 修改本期一条错读，保留冻结的新闻窗口及已核验资料，修复后重新验收。

当前代码限制：非 fixture 的构建会拒绝超过24小时的冻结截止时间，较晚的修订渲染也可能因此被阻止。不要为绕过检查而改写原截止时间；这一限制详见 [采编规则](references/editorial.md)。

真实生产输入包括 `episode.json`、来源文本快照、必要的截图/视频及实际采编记录。schema2 字段、claims与专题回顾授权见 [输入格式](references/episode-format.md)；事实取舍见 [采编规则](references/editorial.md)。截图、译文、卡片和未口播文字同样需要来源映射。

仓库提供一个**明确虚构、禁止发布**的完整结构样例：

```powershell
python -B .\scripts\pipeline.py --profile .\assets\profile.example.json validate --episode .\examples\episode.fixture.json --fixture
python -B .\scripts\pipeline.py --profile .\assets\profile.example.json inventory --episode .\examples\episode.fixture.json --work .\work\fixture-inventory --fixture
```

上述结构命令仅需标准库，不调用模型、不联网、不出成片；它们不等于真实新闻已核验。复制样例制作真实新闻时，需重新取得真实截止时间、来源与采编记录，移除 `fixture`，逐项改写并复核全部内容，不能给样例换日期冒充新闻。

真实生产顺序：先采编并生成、审阅 `inventory`，再 `render`；对最终 MP4 做 `transcribe`、事实复核和媒体检查，完成绑定 manifest 的 `review.json` 后运行 `publish`。具体命令与审阅字段见 [执行与验收](references/operations.md)。

最终交付包含：

- `AI新闻_YYYY-MM-DD_vNN.mp4`、`封面.png`、`字幕.srt`
- `发布文案与来源.md`、`标题.txt`、ZIP 发布包
- 原标题以 `【AI新闻 YYYY-MM-DD】` 结尾；短标题以 `｜M月D日新闻` 结尾，全部字符合计最多20个

中间文件及检查报告在项目 `work/`；正式成品在配置的 `delivery.root`，首版为当日日目录，同日修订另存 v02/v03。禁止伪填审阅 PASS，哈希校验只证明文件未变，不证明事实或听感正确。

## 权限与安全

公开包没有原用户的身份/账号/合集、私人绝对路径、登录会话、日志、成品、模型、字体或订阅密钥。私有配置、缓存、媒体及凭证相关文件通过 `.gitignore` 排除；提交前仍应检查实际暂存内容。

本地制作会读输入、模型和配置，运行 FFmpeg及本地Python子进程，在所选项目写媒体与QA。采编需要读取公开网页；来源快照与当前用户授权记录留在本地work，不放进公开仓库。不需要向本项目提供 GitHub、B站或模型服务密钥。

原参考录屏的点击音效没有确认再分发许可，公开配置默认关闭且不捆绑该声音。若自行启用，配置有使用权的完整文件及 SHA-256；结束点击、短暂停顿之后再进入下一页与口播。外部投稿默认关闭，须当前用户明确要求并自行配置受支持的工具；不沿用原用户登录或长期授权，不绕过本人验证或权限拒绝。

## 验证范围

本次公开副本已在 Windows / Python 3.12 检查技能 frontmatter、全部 Python 语法、JSON/YAML与本地文档引用，验证本地配置生成/拒绝覆盖、虚构样例的 validate/inventory，以及关键拒绝行为（fixture不能作为生产/发布、证据摘录不匹配、来源时间越界）。最终发布文件已扫描私人路径、账号配置和常见凭证模式。

本次没有从零安装完整依赖、下载模型、执行 GPU 配音/全片渲染、做主观听审或平台投稿测试；公开配置关闭音效后也没有生成新成片。保留的制作代码来自现有生产技能，历史运行经验不代替公开副本在新环境的完整验收。首次制作应完成 doctor 和实际成片检查。

## 许可证与来源

本仓库中发布者拥有授权权利的代码与文档按 [MIT](LICENSE) 发布。它依赖 Qwen、sherpa-onnx、SenseVoice、NumPy、Pillow、SoundFile、FFmpeg和Noto等项目，不宣称模型、依赖、字体、素材或整个新闻呈现方式均为原创。MIT不覆盖第三方权利，详细来源与许可证见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
