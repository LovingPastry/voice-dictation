# voice-dictation

Linux 桌面的本地中文语音听写：**按住左 Alt 说话，松开后文字自动输入到光标处。**

- 完全离线：使用 [SenseVoice](https://github.com/FunAudioLLM/SenseVoice)（经 [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) 推理），CPU 即可，25 秒语音识别约 0.4 秒
- 边说边出字：屏幕底部悬浮窗实时显示识别结果，带音量波形
- 自动标点和数字规整，支持中英混说
- 不依赖输入法框架，与 fcitx / ibus / 搜狗等共存

## 环境要求

- Linux + **X11 会话**（Wayland 下无法全局监听按键和模拟粘贴）
- Python 3.9+，或者 conda
- 已在 Ubuntu 20.04 / GNOME 上测试

## 安装

```bash
git clone <本仓库地址> ~/voice-dictation
cd ~/voice-dictation
./install.sh
```

`install.sh` 会依次完成：

1. 用 apt 安装 `xdotool x11-utils libportaudio2 libxcb-cursor0 fonts-noto-cjk`（需要 sudo）
2. 在项目目录创建 `.venv` 并安装 Python 依赖（系统 Python 低于 3.9 时改用 conda）
3. 下载模型（约 230MB，优先 ModelScope，其次 HuggingFace，下载后校验 sha256）
4. 添加开机自启（`~/.config/autostart/voice-dictation.desktop`），并立即启动

## 使用

| 操作 | 效果 |
|---|---|
| 按住左 Alt 超过 0.25 秒 | 开始录音，弹出悬浮窗 |
| 松开左 Alt | 识别完成，文字粘贴到当前光标处 |
| 短按 Alt、Alt+Tab 等组合键 | 照常使用，不触发录音 |
| 录音中按下其他键 | 取消本次听写 |

- 手动启动：`./run.sh`（重复启动会自动退出）
- 日志：`dictation.log`。默认不记录识别出的文字，排查问题时用 `DICTATE_DEBUG=1 ./run.sh` 启动
- 卸载：`./uninstall.sh`，然后删除项目目录

## 工作原理

- **流式预览**：SenseVoice 是非流式模型，但速度很快。按住期间每 0.35 秒对已录音频重新识别一次，所以能边说边出字；超过 12 秒的长段会在停顿处切开，已切出的部分不再重复识别
- **输入文字**：`xdotool type` 输入中文会丢字乱序，所以改为写入剪贴板后模拟 Ctrl+V（终端里用 Ctrl+Shift+V），粘贴后恢复原剪贴板内容
- **防止 Alt 唤出菜单栏**：开始录音时注入一个无映射的 F20 键，避免 Firefox 等程序在松开 Alt 时激活菜单栏

## 自定义

在 `dictate.py` 顶部修改常量：

| 常量 | 默认值 | 说明 |
|---|---|---|
| `HOLD_THRESHOLD` | 0.25 | 按住多久才开始录音（秒） |
| `PREVIEW_INTERVAL` | 0.35 | 悬浮窗刷新间隔（秒） |
| `NUM_THREADS` | 4 | 推理线程数 |
| `TEXT_MAX_WIDTH` / `TEXT_MAX_LINES` | 560 / 2 | 悬浮窗文字区的最大宽度和行数 |

识别语言在 `Recognizer` 中设置，默认是 `language="zh"`。设为 `auto` 时，遇到噪声容易误判成日文或韩文。

## 常见问题

- **按 Alt 没反应**：确认是 X11 会话（`echo $XDG_SESSION_TYPE`），并查看 `dictation.log`
- **识别不准**：换一个好点的麦克风；在系统声音设置里选对输入设备（程序使用默认录音设备）
- **某个程序里粘贴失败**：如果它不响应 Ctrl+V，可以把它的 WM_CLASS（用 `xprop WM_CLASS` 查看）加进 `TERMINAL_CLASSES`

## 许可

MIT。模型 SenseVoice 遵循其[原始许可](https://github.com/FunAudioLLM/SenseVoice/blob/main/LICENSE)，由本程序在安装时下载，不随仓库分发。
