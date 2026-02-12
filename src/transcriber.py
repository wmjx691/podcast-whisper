import os
import time
import json
import sys
from faster_whisper import WhisperModel
from typing import Optional
from tqdm import tqdm  # <--- 新增：引入進度條套件

# --- 新增：環境設定區 ---
def detect_environment():
    """偵測是否在 Colab 環境"""
    # 1. 檢查是否有 Colab 特有的環境變數 (適用於 !python 腳本執行)
    if "COLAB_RELEASE_TAG" in os.environ or "COLAB_GPU" in os.environ:
        return True
    
    # 2. 檢查 sys.modules (適用於 Notebook 互動模式)
    if 'google.colab' in sys.modules:
        return True
        
    return False

def get_paths():
    """根據環境回傳正確的專案根目錄與音訊路徑"""
    if detect_environment():
        print("☁️ 偵測到 Colab 環境")
        from google.colab import drive
        # 強制掛載 Google Drive
        if not os.path.exists('/content/drive'):
            drive.mount('/content/drive')
        
        # ⚠️ 注意：這裡假設您將專案上傳到了 Drive 的 "MyProject/whisper" 資料夾
        # 請根據您實際的 Drive 結構修改這裡！
        project_root = '/content/drive/MyDrive/MyProject/whisper'
    else:
        print("💻 偵測到本地環境")
        # 取得目前檔案 (transcriber.py) 的上一層的上一層
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    audio_dir = os.path.join(project_root, "data", "audio")
    return project_root, audio_dir

# --- 原有的類別邏輯 (微調) ---
class PodcastTranscriber:
    def __init__(self, model_size: str = "large-v3", device: str = "auto", compute_type: str = "float16"):
        # 1. 取得專案根目錄 (我們之前寫的 detect_environment 邏輯會決定這是本地還是雲端路徑)
        project_root, _ = get_paths()
        
        # 2. 設定模型存放路徑：存在專案底下的 "models" 資料夾
        # 例如在 Colab 上會是：/content/drive/MyDrive/MyProject/whisper/models
        model_root = os.path.join(project_root, "models")
        
        # 確保資料夾存在
        if not os.path.exists(model_root):
            os.makedirs(model_root)

        print(f"🚀 正在載入 Whisper 模型: {model_size} ({device}) | 精度: {compute_type}...")
        print(f"📂 模型快取路徑: {model_root}")

        try:
            # 3. 關鍵修改：加入 download_root 參數
            self.model = WhisperModel(
                model_size, 
                device=device, 
                compute_type=compute_type,
                download_root=model_root  # <--- 就是這一行！
            )
            print("✅ 模型載入完成！")
        except Exception as e:
            print(f"❌ 模型載入失敗: {e}")
            raise

    def transcribe_file(self, audio_path: str) -> Optional[str]:
        """
        轉錄單個音訊檔案，輸出 txt 和 json
        """
        if not os.path.exists(audio_path):
            print(f"❌ 錯誤：找不到檔案 {audio_path}")
            return None

        file_name = os.path.basename(audio_path)
        # 輸出路徑改為相對路徑，確保跟隨 audio_path
        output_dir = os.path.join(os.path.dirname(audio_path), "../transcripts")
        
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        base_name = os.path.splitext(file_name)[0]
        txt_path = os.path.join(output_dir, f"{base_name}.txt")
        json_path = os.path.join(output_dir, f"{base_name}.json")

        # 檢查是否已經轉錄過 (避免重複執行)
        if os.path.exists(txt_path) and os.path.exists(json_path):
            print(f"⏭️  跳過已轉錄檔案: {file_name}")
            return txt_path

        print(f"\n🎙️  開始轉錄: {file_name}")
        start_time = time.time()

        try:
            # 1. 取得 segments 生成器 與 音檔資訊
            segments, info = self.model.transcribe(
                audio_path, 
                beam_size=5, 
                language="zh", 
                vad_filter=True
            )

            print(f"   ℹ️  語言: {info.language} (信心度: {info.language_probability:.2f}) | 長度: {info.duration:.2f}s")
            
            transcript_data = []
            
            # 使用 list 暫存，最後一次寫入，減少 IO (Colab 上 Drive 的 IO 比較慢)
            full_text_lines = []
            
            # 寫入檔頭
            full_text_lines.append(f"來源: {file_name}")
            full_text_lines.append(f"模型: large-v3 | 時間: {time.strftime('%Y-%m-%d %H:%M:%S')}")
            full_text_lines.append("-" * 50 + "\n")

            # --- 2. 使用 tqdm 顯示進度條 ---
            # total=info.duration : 設定進度條總長度為音檔秒數
            # unit='s' : 單位顯示為秒
            with tqdm(total=round(info.duration, 2), unit='s', desc="   Processing", leave=True) as pbar:
                for i, segment in enumerate(segments, 1):
                    start_m, start_s = divmod(int(segment.start), 60)
                    end_m, end_s = divmod(int(segment.end), 60)
                    time_str = f"[{start_m:02d}:{start_s:02d} -> {end_m:02d}:{end_s:02d}]"
                    text = segment.text.strip()
                    
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

            # 3. 寫入檔案
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

    def transcribe_folder(self, folder_path: str):
        """
        批次轉錄資料夾內的所有音訊檔案
        """
        if not os.path.exists(folder_path):
            print(f"❌ 資料夾不存在: {folder_path}")
            return

        # 支援的音訊格式
        audio_extensions = ('.mp3', '.m4a', '.wav', '.flac')
        # 找出所有音訊檔
        files = [f for f in os.listdir(folder_path) if f.lower().endswith(audio_extensions)]
        files.sort() # 排序，確保順序一致
        
        print(f"\n📂 處理資料夾: {folder_path} (共 {len(files)} 個檔案)")
        for f in files:
            self.transcribe_file(os.path.join(folder_path, f))

# --- 主程式區 ---
if __name__ == "__main__":
    # 1. 自動取得路徑
    PROJECT_ROOT, AUDIO_DIR = get_paths()
    
    # 2. 設定模型參數
    # 如果是 Colab (有 GPU)，我們用 float16 跑比較快；本地 CPU 用 int8
    is_colab = detect_environment()
    device = "cuda" if is_colab else "cpu"
    compute_type = "float16" if is_colab else "int8"
    
    # 3. 初始化轉錄器
    transcriber = PodcastTranscriber(
        model_size="large-v3", 
        device=device, 
        compute_type=compute_type
    )
    
    # 4. 執行轉錄
    # 這裡會自動掃描 AUDIO_DIR 下的所有檔案
    transcriber.transcribe_folder(AUDIO_DIR)