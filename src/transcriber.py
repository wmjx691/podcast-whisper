import os
import time
import json
from faster_whisper import WhisperModel
from typing import Optional, List

class PodcastTranscriber:
    def __init__(self, model_size: str = "large-v3", device: str = "auto", compute_type: str = "int8"):
        """
        初始化轉錄器
        :param model_size: 模型大小 (建議用 large-v3 以獲得最佳中文效果)
        :param device: "cpu" 或 "cuda"
        :param compute_type: "int8" (省記憶體關鍵)
        """
        print(f"🚀 正在載入 Whisper 模型: {model_size} ({device})...")
        self.model = WhisperModel(model_size, device=device, compute_type=compute_type)
        print("✅ 模型載入完成！")

    def transcribe_file(self, audio_path: str) -> Optional[str]:
        """
        轉錄單個音訊檔案，輸出 txt 和 json
        """
        if not os.path.exists(audio_path):
            print(f"❌ 錯誤：找不到檔案 {audio_path}")
            return None

        file_name = os.path.basename(audio_path)
        
        # 準備輸出路徑
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
            segments, info = self.model.transcribe(
                audio_path, 
                beam_size=5, 
                language="zh", 
                vad_filter=True
            )

            print(f"   ℹ️  語言: {info.language} (信心度: {info.language_probability:.2f}) | 長度: {info.duration:.2f}s")
            
            transcript_data = []

            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(f"來源: {file_name}\n")
                f.write(f"模型: large-v3 | 時間: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("-" * 50 + "\n\n")

                for i, segment in enumerate(segments, 1):
                    start_m, start_s = divmod(int(segment.start), 60)
                    end_m, end_s = divmod(int(segment.end), 60)
                    time_str = f"[{start_m:02d}:{start_s:02d} -> {end_m:02d}:{end_s:02d}]"
                    text = segment.text.strip()
                    
                    line = f"{time_str} {text}"
                    f.write(line + "\n")
                    
                    transcript_data.append({
                        "id": i,
                        "start": segment.start,
                        "end": segment.end,
                        "text": text
                    })

                    # 每 20 句印一次進度
                    if i % 20 == 0:
                        print(f"   -> 處理中: {time_str}")

            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(transcript_data, f, ensure_ascii=False, indent=2)

            duration = time.time() - start_time
            print(f"✅ 完成！耗時: {duration:.2f}s")
            return txt_path

        except Exception as e:
            print(f"❌ 失敗: {file_name} - {e}")
            return None

    def transcribe_folder(self, folder_path: str) -> None:
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
        
        total_files = len(files)
        print(f"\n📂 準備處理資料夾: {folder_path}")
        print(f"📊 共發現 {total_files} 個音訊檔案")
        print("=" * 50)

        for index, file_name in enumerate(files, 1):
            print(f"\n[{index}/{total_files}] 處理檔案: {file_name}")
            audio_path = os.path.join(folder_path, file_name)
            self.transcribe_file(audio_path)
            
        print("\n🎉 所有檔案處理完畢！")

# --- 測試區 ---
if __name__ == "__main__":
    # 初始化 (如果您覺得 large-v3 太慢，這裡可以改回 small)
    transcriber = PodcastTranscriber(model_size="small", device="cpu", compute_type="int8")
    
    # 指定要處理的資料夾
    audio_folder = "data/audio/openhouse"
    
    transcriber.transcribe_folder(audio_folder)