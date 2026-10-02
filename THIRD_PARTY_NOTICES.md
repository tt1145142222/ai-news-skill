# 第三方来源与许可证

项目 LICENSE 的 MIT 授权仅适用于发布者有权授权的项目代码与文档，不替代或重新授权以下第三方内容。公开仓库没有捆绑第三方包源码、二进制、模型权重、字体或参考视频素材；安装取得的资源仍需保留它们自己的版权、许可证及 NOTICE。

## 外部运行依赖

| 项目 | 用途 | 上游许可证与来源 |
| --- | --- | --- |
| Qwen3-TTS | 本地 CustomVoice / Dylan 配音 | [QwenLM/Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS)，代码 [Apache-2.0](https://github.com/QwenLM/Qwen3-TTS/blob/main/LICENSE)；[所选模型卡](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice) 标示 Apache-2.0，权重不随仓库分发 |
| PyTorch | Qwen GPU推理 | [BSD-3-Clause及所含第三方声明](https://github.com/pytorch/pytorch/blob/main/LICENSE)，以安装构建的完整许可为准 |
| sherpa-onnx | SenseVoice本地识别 | [Apache-2.0](https://github.com/k2-fsa/sherpa-onnx/blob/master/LICENSE) |
| SenseVoice | 转写模型技术与导出资源 | [SenseVoice项目代码 MIT](https://github.com/QwenAudio/SenseVoice/blob/main/LICENSE)；[选用的ONNX导出包](https://huggingface.co/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09) 由其发布者提供，具体权重许可与上游模型说明须在下载时核对，不能仅用项目代码许可证推定权重许可 |
| NumPy | 音频数组与信号处理 | [BSD-3-Clause](https://github.com/numpy/numpy/blob/main/LICENSE.txt) |
| Pillow / PIL | 本地卡片、字幕、封面与QA图像 | [MIT-CMU](https://github.com/python-pillow/Pillow/blob/main/LICENSE)，保留原PIL/Pillow版权声明 |
| SoundFile | 音频文件读写 | [BSD-3-Clause](https://github.com/bastibe/python-soundfile/blob/master/LICENSE)；其依赖 libsndfile 的权利独立，见 [libsndfile许可](https://github.com/libsndfile/libsndfile/blob/master/COPYING) |
| imageio-ffmpeg | 定位与调用FFmpeg | Python包装器 [BSD-2-Clause](https://github.com/imageio/imageio-ffmpeg/blob/main/LICENSE)；FFmpeg二进制不受包装器许可证重新授权 |
| FFmpeg | 编解码、混音、响度测量和抽帧 | [上游法律与许可说明](https://ffmpeg.org/legal.html)：通常 LGPL-2.1-or-later，启用GPL组件时适用GPL；具体以实际构建选项及所含组件为准 |
| Noto Sans CJK | 中文排版 | [SIL Open Font License 1.1](https://github.com/notofonts/noto-cjk/blob/main/Sans/LICENSE)，字体不捆绑、不使用原机器的系统宋体 |

旧schema1复现分支和音频模块仍包含Kokoro接口兼容代码，当前公开默认使用Qwen，不携带Kokoro模型或数据。启用旧分支时需自行取得对应模型、语音数据和G2P组件，核对它们各自的许可证；不能将本表的Qwen或项目MIT视为这些资源的许可。

## 呈现参考与排除的素材

该技能的蓝色新闻卡片、资料窗口、导航和核验规则包含本地制作经验及对外部新闻视频呈现的观察。原安装目录曾包含第三方录屏截取的点击声音、原网页资料联系表、历史视频抽帧和样音；现有记录没有确定可公开再分发的许可证或完整权利信息。这些音频、图片、原录屏和相关私人来源路径均已排除，公开默认关闭点击声音。参考观察不等于取得素材版权，也不证明外部作者的后台采编或音源。

新闻实际制作时使用的原帖、页面截图、品牌图、演示视频和引文由各自权利人保有权利。每期取得真实来源并记录出处和适用限制；本项目的MIT授权不授权第三方新闻素材。新加入可分发素材或第三方代码时，保留其真实来源、原版权、许可证与必要的NOTICE，不把它们统一改成MIT。
