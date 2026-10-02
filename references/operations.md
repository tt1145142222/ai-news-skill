# 本地执行与验收

## 配置与依赖

按根目录 README 安装运行依赖、Qwen GPU 环境、SenseVoice模型与 Noto Sans CJK 字体。`scripts/configure.py` 从公开样例生成绝对路径的 `config/profile.local.json`；路径按安装者选择，私有配置不提交 Git。Qwen 配音、模型权重、FFmpeg和字体由各自上游提供，仓库不捆绑。Qwen推理使用 CUDA、bfloat16 与 SDPA，未实现 CPU 回退。转写模型目录为 `models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09`，至少包含 `model.int8.onnx` 与 `tokens.txt`。

`doctor` 检查依赖、可写性、输出盘不少于10GiB且满足预计产物两倍空间，以及成品目的地空间；会创建配置所指输出/成品目录并短暂测试写入。备用目录由本地配置指定。不会删除历史成品腾空间。主运行环境与 Qwen 环境隔离，已有环境无需每天重装。

## 生产命令

下列命令在技能目录执行，`$newsProject` 为自行选定的项目目录，新闻输入由代理按 episode-format 编写：

```powershell
$newsProject = (Resolve-Path '..\news-runtime').Path
$newsPython = Join-Path $newsProject 'tools\venv\Scripts\python.exe'
$newsEpisode = Join-Path $newsProject 'work\current\episode.json'
& $newsPython .\scripts\pipeline.py doctor
& $newsPython .\scripts\pipeline.py inventory --episode $newsEpisode --work (Join-Path $newsProject 'work\current\inventory')
# 实际审阅事实与发音清单后再渲染
& $newsPython .\scripts\pipeline.py render --episode $newsEpisode
```

可用 `--profile`（放在子命令前）指定其他本地配置。`inventory` 生成 pronunciation-inventory.json 和 factual-inventory.json；不合成声音，不证明事实已通过。`render` 内含 doctor、结构/证据映射/标题检查，会创建独立 work，生成 manifest、媒体报告、待审 review 和 final-qa。修改用新 work，冻结窗口不随配音/渲染重试移动。

在已配置项目 work 查必要的 `delivery.json` 与相应 episode/manifest/review，排除 fixtures、演练和备份；按 `episode.cutoff` 选小于本期 T 的最近有效不同一期 P，不按文件日期、上传或审核时刻。只渲染未交付不算上期。把选定路径填入 `news_window.previous_delivery`；无可靠上期写 null 与真实 fallback_reason。程序验证锚点身份和哈希，不证明代理已选择“最近”一期。跨期事件必须重新核验新增。

点击默认关闭。若启用，配置 `transition_sound.asset_path` 的自有/许可音效路径及完整 `asset_sha256`；相对音效路径基于技能目录，绝对路径按提供值使用。只在可再分发或自用授权明确时配置，不采用已排除的原录屏片段。其完整时长须容纳在上一句结束后的停顿与下一页开始之间；必要时调整停顿与 start_offset，不截尾或静默覆盖口播。BGM 默认关闭，显式启用后使用脚本内本地合成器。

## 实际检查与审阅

对同一最终视频检查所有 claim 的原始上下文、日期、数字基线、归因及限定，覆盖标题/卡片/封面/字幕/译文与发布文案。独立冷审或第二遍复核不能继承上期 PASS。查看 contact、final-qa 联系表及必要原尺寸帧，检查资料进出与动态、概览滚动、最长标题/专名、全部导航、告别、来源行、字幕和转场；技术测量不代替看图或事实判断。

```powershell
& $newsPython .\scripts\pipeline.py transcribe --work $newsWork
```

`$newsWork` 使用 render 实际返回的绝对目录。转写绑定最终MP4的哈希；ASR相似、无削波不能证明主观自然度。无听辨用 `asr-and-signal` 并保留局限。已知发音缺陷重配受影响完整句、重算时间线并重新做相关检查。

`review.json` 初始为待审，逐项实际核验后记录 reviewer、claim_checks、scene_checks、media_checks、原文/日期/限定、全层事实、发音、字幕时序、新闻状态/排序和 final_media_qa_verified。每条新闻的 media_selection_notes 写实际选择/省略理由；social_source_notes 写真实X/论坛/评论覆盖及访问限制；editorial_research_notes 写实际查漏/排除/核心新增。高风险证据和专题回顾按 episode-format 记录，不能由代理编造用户授权。issues 仅在真实问题解决后清空。全部审阅绑定当前 manifest 和被追踪文件哈希，变更后重新验收，不能替换 hash 绕过。

```powershell
& $newsPython .\scripts\pipeline.py publish --work $newsWork
```

`publish` 只做本地复制与ZIP打包，拒绝历史fixture和待审/过期review。成品目录来自冻结profile.delivery.root；首版放当日日目录，修订放 v02/v03，不覆盖旧版。双标题在输入、标题.txt与文案中一致。平台投稿另按 platform-publishing。

## 演练和复用

仓库 examples 为明确标注的虚构历史结构演练，`--fixture` 才可校验/渲染，不能发布或充当上期生产。公开副本未完成 GPU全流程渲染测试，真实节目仍需本期事实和媒体检查。`--reuse-video-work` 仅在旧work已有有效审阅/交付、画面资料字幕与时间线全部一致时复用 H264流，仍生成新 manifest 并检查最终AAC与帧；任一不符走完整渲染。维护范围见 iteration。
