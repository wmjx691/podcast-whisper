import os
import time
import json
import sys
from faster_whisper import WhisperModel
from typing import Optional
from functools import wraps
from transcript_candidate import checkpoint, current_execution
from opencc import OpenCC

from utils import get_project_root, detect_environment


def _observed(stage, *, none_is_failure=False):
    def decorate(operation):
        @wraps(operation)
        def invoke(*args, **kwargs):
            with checkpoint(stage) as context:
                result = operation(*args, **kwargs)
                if none_is_failure and result is None:
                    context.event(stage, "FAIL")
                return result
        return invoke
    return decorate


# --- 核心轉錄類別 ---
class PodcastTranscriber:
    @_observed("transcriber_init")
    def __init__(self, model_size: str, device: str, compute_type: str):
        project_root = get_project_root()
        model_root = os.path.join(project_root, "models")
        
        # s2twp 代表：Simplified to Traditional (Taiwan) with Phrases (包含台灣慣用語轉換)
        self.cc = OpenCC('s2twp')
        self.model_size = model_size  # 🌟 新增：把模型大小存起來，之後寫 Metadata 會用到

        if not os.path.exists(model_root):
            os.makedirs(model_root)

        
        try:
            self.model = WhisperModel(
                model_size, 
                device=device, 
                compute_type=compute_type,
                download_root=model_root
            )
        except Exception:
            raise

    # 🌟 新增：加入 force_retranscribe 參數
    @_observed("transcription", none_is_failure=True)
    def transcribe_file(self, audio_path: str, output_dir: str, language: str, initial_prompt: str, force_retranscribe: bool = False) -> Optional[str]:
        if not os.path.exists(audio_path):
            return None

        file_name = os.path.basename(audio_path)
        
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        base_name = os.path.splitext(file_name)[0]
        txt_path = os.path.join(output_dir, f"{base_name}.txt")
        json_path = os.path.join(output_dir, f"{base_name}.json")

        # 🌟 修改：跳過邏輯加上 force_retranscribe 的判斷
        if os.path.exists(txt_path) and os.path.exists(json_path):
            if not force_retranscribe:
                return txt_path
        start_time = time.time()

        try:
            # 這裡把 condition_on_previous_text 設為 False，能大幅減少「幻覺迴圈」
            segments, info = self.model.transcribe(
                audio_path, 
                beam_size=5, 
                language=language, 
                vad_filter=True,
                initial_prompt=initial_prompt,
                condition_on_previous_text=False 
            )

            
            segments_data = [] # 用來存純對話片段
            full_text_lines = []
            
            full_text_lines.append(f"來源: {file_name}")
            # 🌟 修改：動態填入真實的模型大小，不再寫死 large-v3
            full_text_lines.append(f"模型: {self.model_size} | 時間: {time.strftime('%Y-%m-%d %H:%M:%S')}")
            full_text_lines.append("-" * 50 + "\n")

            # --- 變數初始化：去重邏輯 ---
            last_text = "" 
            repeat_count = 0
            MAX_REPEATS = 1   # 允許重複幾次？ 1 代表允許出現兩次 (原句 + 1次重複)

            for i, segment in enumerate(segments, 1):
                current_execution().progress(segment.end, info.duration)
                raw_text = segment.text.strip()

                # --- 強制轉繁體 ---
                text = self.cc.convert(raw_text)

                # --- 改良版去重邏輯 ---
                if text == last_text:
                    repeat_count += 1
                else:
                    repeat_count = 0  # 內容不同，重置計數器

                last_text = text # 更新上一句記錄

                # 如果重複次數超過閾值，則跳過 (視為幻覺)
                if repeat_count > MAX_REPEATS:
                    continue

                start_m, start_s = divmod(int(segment.start), 60)
                end_m, end_s = divmod(int(segment.end), 60)
                time_str = f"[{start_m:02d}:{start_s:02d} -> {end_m:02d}:{end_s:02d}]"

                line = f"{time_str} {text}"
                full_text_lines.append(line)

                segments_data.append({
                    "id": i,
                    "start": segment.start,
                    "end": segment.end,
                    "text": text
                })


            # 寫入 TXT
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write("\n".join(full_text_lines))

            # 🌟 新增：建構包含 Metadata 的全新 JSON 結構
            env_name = "Colab" if detect_environment() else "Local/GitHub Actions"
            final_json_data = {
                "metadata": {
                    "model_size": self.model_size,
                    "environment": env_name,
                    "language": info.language,
                    "prompt": initial_prompt,
                    "timestamp": time.strftime('%Y-%m-%dT%H:%M:%S')
                },
                "segments": segments_data
            }

            # 寫入 JSON
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(final_json_data, f, ensure_ascii=False, indent=2)

            duration = time.time() - start_time
            return txt_path

        except Exception:
            return None

    # 🌟 修改：加入 force_retranscribe 參數並往下傳遞
    def transcribe_folder(self, folder_path: str, output_path: str, language: str, prompt: str, force_retranscribe: bool = False):
        if not os.path.exists(folder_path):
            print(f"❌ 資料夾不存在: {folder_path}")
            return

        audio_extensions = ('.mp3', '.m4a', '.wav', '.flac')
        files = [f for f in os.listdir(folder_path) if f.lower().endswith(audio_extensions)]
        files.sort()
        
        print(f"\n📂 處理資料夾: {folder_path} (共 {len(files)} 個檔案)")
        print(f"📂 輸出位置: {output_path}")
        
        for f in files:
            self.transcribe_file(
                audio_path=os.path.join(folder_path, f),
                output_dir=output_path,
                language=language,
                initial_prompt=prompt,
                force_retranscribe=force_retranscribe
            )

# --- 主程式區 (User Configuration) ---
if __name__ == "__main__":
    # 1. 取得專案根目錄
    PROJECT_ROOT = get_project_root()
    
    # 2. --- 使用者設定區 (User Config) ---
    # 您可以在這裡自由修改，完全不用動到上面的程式碼
    
    # [設定] 模型大小
    MODEL_SIZE = "small"  # 可選 tiny, base, small, medium, large-v3 (視硬體能力而定)
    
    # [設定] 音檔輸入與輸出位置
    INPUT_AUDIO_DIR = os.path.join(PROJECT_ROOT, "data", "audio", "openhouse")
    OUTPUT_TRANSCRIPT_DIR = os.path.join(PROJECT_ROOT, "data", "transcripts", "openhouse")
    
    # [設定] 轉錄參數
    # 如果您的音檔不一定是繁中，這裡可以設為 None，讓模型自動偵測語言
    # TARGET_LANGUAGE = None 
    TARGET_LANGUAGE = "zh" 
    
    # Prompt 可以引導模型選字 (例如專有名詞)，也可以設為 None
    INITIAL_PROMPT = "這是一段Podcast對話。請將語音內容準確轉錄為繁體中文。"
    # ------------------------------------
    
    # 3. 自動偵測環境
    is_colab = detect_environment()
    device = "cuda" if is_colab else "cpu"
    compute_type = "float16" if is_colab else "int8"
    
    print(f"🔍 環境: {'Colab (GPU)' if is_colab else 'Local (CPU)'}")
    if TARGET_LANGUAGE:
        print(f"🎯 指定語言: {TARGET_LANGUAGE}")
    else:
        print(f"🌍 語言模式: 自動偵測")

    # 4. 初始化並執行
    transcriber = PodcastTranscriber(
        model_size=MODEL_SIZE, 
        device=device, 
        compute_type=compute_type
    )
    
    transcriber.transcribe_folder(
        folder_path=INPUT_AUDIO_DIR,
        output_path=OUTPUT_TRANSCRIPT_DIR,
        language=TARGET_LANGUAGE,
        prompt=INITIAL_PROMPT
    )