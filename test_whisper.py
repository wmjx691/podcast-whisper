from faster_whisper import WhisperModel
import time

# --- 設定區 ---
# 模型選擇: "large-v3", "medium", "small"
# 建議您先用 "small" 測試環境是否跑通，確認沒問題再換 "large-v3"
MODEL_SIZE = "small" 

# 執行裝置: "cuda" (NVIDIA顯卡) 或 "cpu"
# int8 量化可以大幅減少記憶體使用，讓筆電 CPU 也能勉強跑 Large 模型
device = "cpu"  
compute_type = "int8" 

def main():
    print(f"1. 正在載入模型: {MODEL_SIZE} ({device})...")
    # download_root 可以指定模型下載路徑，避免每次都重新下載
    model = WhisperModel(MODEL_SIZE, device=device, compute_type=compute_type)

    print("2. 模型載入完成！準備轉錄...")
    
    # 這裡請準備一個簡單的 mp3 檔案放在同目錄下，例如 "test.mp3"
    # 如果沒有檔案，程式會報錯
    audio_file = "audio_test_English.m4a" 
    
    try:
        start_time = time.time()
        
        # --- 核心轉錄函式 ---
        # beam_size: 搜尋廣度，5 是標準值
        segments, info = model.transcribe(
            audio_file, 
            beam_size=5, 
            word_timestamps=True,  # 啟用單字級對齊
            vad_filter=True
        )
        print(f"   偵測語言: {info.language} (信心度: {info.language_probability:.2f})")
        print(f"   音訊長度: {info.duration:.2f} 秒")
        print("-" * 30)

        # segments 是一個產生器 (Generator)，必須用迴圈跑才會開始真正轉錄
        for segment in segments:
            # 格式化輸出： [MM:SS] 內容
            start_min = int(segment.start // 60)
            start_sec = int(segment.start % 60)
            end_min = int(segment.end // 60)
            end_sec = int(segment.end % 60)

            # 顯示段落
            print(f"[{start_min:02d}:{start_sec:02d} -> {end_min:02d}:{end_sec:02d}] {segment.text}")
            
            # 如果您想看更細的，可以把下面這行註解打開，會列出每個字的秒數
            # for word in segment.words:
            #     print(f"   ({word.start:.2f}-{word.end:.2f}) {word.word}")

        end_time = time.time()
        print("-" * 30)
        print(f"轉錄完成！耗時: {end_time - start_time:.2f} 秒")

    except Exception as e:
        print(f"發生錯誤: {e}")
        print("提示：請確認目錄下是否有 'test.mp3' 檔案")

if __name__ == "__main__":
    main()