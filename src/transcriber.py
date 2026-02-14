import os
import time
import json
import sys
from faster_whisper import WhisperModel
from typing import Optional
from tqdm import tqdm
from opencc import OpenCC

# --- 環境與路徑輔助函式 ---
def detect_environment():
    """偵測是否在 Colab 環境"""
    return "COLAB_RELEASE_TAG" in os.environ or 'google.colab' in sys.modules

def get_project_root():
    """回傳專案根目錄"""
    if detect_environment():
        if os.path.exists('/content/drive'):
             pass
        else:
             print("⚠️ 注意：在腳本模式下無法互動掛載 Drive，請確保外部 Notebook 已執行 drive.mount()")
        # ⚠️ 請確認您的 Drive 路徑是否正確
        return '/content/drive/MyDrive/MyProject/whisper'
    else:
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --- 核心轉錄類別 ---
class PodcastTranscriber:
    def __init__(self, model_size: str, device: str, compute_type: str):
        project_root = get_project_root()
        model_root = os.path.join(project_root, "models")
        
        # s2twp 代表：Simplified to Traditional (Taiwan) with Phrases (包含台灣慣用語轉換)
        self.cc = OpenCC('s2twp')

        if not os.path.exists(model_root):
            os.makedirs(model_root)

        print(f"🚀 正在載入 Whisper 模型: {model_size} ({device}) | 精度: {compute_type}...")
        
        try:
            self.model = WhisperModel(
                model_size, 
                device=device, 
                compute_type=compute_type,
                download_root=model_root
            )
            print("✅ 模型載入完成！")
        except Exception as e:
            print(f"❌ 模型載入失敗: {e}")
            raise

    def transcribe_file(self, audio_path: str, output_dir: str, language: str, initial_prompt: str) -> Optional[str]:
        if not os.path.exists(audio_path):
            print(f"❌ 錯誤：找不到檔案 {audio_path}")
            return None

        file_name = os.path.basename(audio_path)
        
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        base_name = os.path.splitext(file_name)[0]
        txt_path = os.path.join(output_dir, f"{base_name}.txt")
        json_path = os.path.join(output_dir, f"{base_name}.json")

        if os.path.exists(txt_path) and os.path.exists(json_path):
            print(f"⏭️  跳過已轉錄檔案: {file_name}")
            return txt_path

        print(f"\n🎙️  開始轉錄: {file_name}")
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

            print(f"   ℹ️  語言: {info.language} | 總長度: {info.duration:.2f} 秒")
            
            transcript_data = []
            full_text_lines = []
            
            full_text_lines.append(f"來源: {file_name}")
            full_text_lines.append(f"模型: large-v3 | 時間: {time.strftime('%Y-%m-%d %H:%M:%S')}")
            full_text_lines.append("-" * 50 + "\n")

            # --- 變數初始化：去重邏輯 ---
            last_text = "" 
            repeat_count = 0
            MAX_REPEATS = 1  # 允許重複幾次？ 1 代表允許出現兩次 (原句 + 1次重複)

            # 設定進度條
            with tqdm(total=round(info.duration, 2), unit='s', desc="Processing", leave=True, ascii=True, ncols=100) as pbar:
                for i, segment in enumerate(segments, 1):
                    raw_text = segment.text.strip()
                    
                    # --- 新增：強制轉繁體 ---
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
                    # -----------------------

                    start_m, start_s = divmod(int(segment.start), 60)
                    end_m, end_s = divmod(int(segment.end), 60)
                    time_str = f"[{start_m:02d}:{start_s:02d} -> {end_m:02d}:{end_s:02d}]"
                    
                    line = f"{time_str} {text}"
                    full_text_lines.append(line)
                    
                    transcript_data.append({
                        "id": i,
                        "start": segment.start,
                        "end": segment.end,
                        "text": text
                    })

                    # 更新進度條
                    # segment.end 是目前這句話結束的時間點
                    # 我們將進度條更新到這個時間點
                    pbar.update(segment.end - pbar.n)

            with open(txt_path, "w", encoding="utf-8") as f:
                f.write("\n".join(full_text_lines))

            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(transcript_data, f, ensure_ascii=False, indent=2)

            duration = time.time() - start_time
            print(f"✅ 完成！耗時: {duration:.2f}s")
            return txt_path

        except Exception as e:
            print(f"❌ 失敗: {file_name} - {e}")
            return None

    def transcribe_folder(self, folder_path: str, output_path: str, language: str, prompt: str):
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
                initial_prompt=prompt
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
    INITIAL_PROMPT = "這是一段台灣閩南語與國語的混合對話。請將台語內容準確轉錄為繁體中文。"
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