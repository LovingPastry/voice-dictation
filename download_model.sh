#!/bin/sh
# 下载 SenseVoice int8 模型（约 230MB）。优先 ModelScope，失败再试 HuggingFace 及其镜像
set -e
DIR="$(cd "$(dirname "$0")" && pwd)/models/sense-voice"
mkdir -p "$DIR"
MS=https://www.modelscope.cn/models/pengzhendong/sherpa-onnx-sense-voice-zh-en-ja-ko-yue/resolve/master
HF=https://huggingface.co/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main
HFM=https://hf-mirror.com/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main

fetch() {  # fetch <文件名> <sha256>
    f=$1; sum=$2
    if [ -f "$DIR/$f" ] && echo "$sum  $DIR/$f" | sha256sum -c --status; then
        echo "已存在: $f"; return 0
    fi
    for base in $MS $HF $HFM; do
        echo "下载 $f <- $base"
        if curl -fL -# --retry 2 -o "$DIR/$f.part" "$base/$f" \
           && echo "$sum  $DIR/$f.part" | sha256sum -c --status; then
            mv "$DIR/$f.part" "$DIR/$f"; return 0
        fi
        echo "  失败或校验不通过，换下一个源"
    done
    rm -f "$DIR/$f.part"; echo "下载 $f 失败" >&2; return 1
}
fetch tokens.txt      f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc
fetch model.int8.onnx c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51
echo "模型就绪: $DIR"
