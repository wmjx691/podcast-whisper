from dotenv import load_dotenv
load_dotenv() # 這行會自動把 .env 裡面的東西載入到環境變數中
import os
import sys
import re

# 將 src 目錄加入系統路徑，方便載入模組
sys.path.append(os.path.join(os.path.dirname(__file__), "src"))

from idempotency_checker import IdempotencyChecker
from rss_parser import PodcastDownloader
from transcriber import PodcastTranscriber, detect_environment
from upload_to_drive import upload_files_to_drive, get_project_root
from yt_downloader import YouTubeDownloader

def main():
    print("========== 🚀 開始自動化 Podcast 轉錄流程 ==========")
    
    # --- 參數設定區 ---
    SOURCE_TYPE = "podcast" # 可切換為 "podcast" 或 "youtube"
    
    # [Podcast 專用設定]
    # 歐本豪斯 的 RSS Feed URL (可以替換成其他 Podcast 的 RSS)
    RSS_URL = "https://feed.firstory.me/rss/user/cke0tqspfvlc00803lwhmdb2t"
    PODCAST_NAME = "openhouse" # 資料夾名稱

    # # 股癌 的 RSS Feed URL
    # RSS_URL = "https://feeds.soundon.fm/podcasts/954689a5-3096-43a4-a80b-7810b219cef3.xml" 
    # PODCAST_NAME = "gooaye" # 資料夾名稱
    
    # [YouTube 專用設定]
    YOUTUBE_URL = "https://www.youtube.com/watch?v=zglsl9w4u_0" # 貼上您想轉錄的網址
    YOUTUBE_FOLDER_NAME = "youtube_collection" # 雲端會自動建立這個資料夾存放逐字稿
    
    # 🌟 總司令的控制面板 (User Control Panel) 🌟
    TARGET_MODEL = "small"         # 想升級品質時，可以改成 "medium" 或 "large-v3"
    FORCE_RETRANSCRIBE = False     # 設為 True 時，會無視已存在的舊檔案，強制全部重新轉錄覆蓋

    # 🌟 新增：將語言與 Prompt 抽離到控制面板，方便切換！
    TARGET_LANGUAGE = "zh" # 若是中文請填 "zh"，不知道就填 None 讓模型自動偵測
    INITIAL_PROMPT = "這是一段Podcast對話。請將語音內容準確轉錄為繁體中文。"    # 轉錄英文歌不需要中文 prompt，請留空或填入英文提示
    
    DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID")
    if not DRIVE_FOLDER_ID:
        raise ValueError("❌ 找不到 DRIVE_FOLDER_ID！請確保已設定環境變數。")
    
    # 1. 初始化路徑
    target_name = PODCAST_NAME if SOURCE_TYPE == "podcast" else YOUTUBE_FOLDER_NAME
    project_root = get_project_root()
    audio_dir = os.path.join(project_root, "data", "audio", target_name)
    transcript_dir = os.path.join(project_root, "data", "transcripts", target_name)
    
    # 確保資料夾存在
    os.makedirs(audio_dir, exist_ok=True)
    os.makedirs(transcript_dir, exist_ok=True)

    # --- 步驟 0：情報蒐集 (Idempotency Check) ---
    print("\n>> [步驟 0/4]: 啟動情報局，蒐集本地與雲端狀態...")
    checker = IdempotencyChecker(audio_dir, transcript_dir, DRIVE_FOLDER_ID, target_name)
    status_report = checker.get_comprehensive_status()

# --- 先建立雲端已存在的 EP 數字清單 ---
    drive_eps = set()
    for base_name, info in status_report.items():
        # 先把 transcript 的狀態取出來
        transcript_info = info.get("transcript")
        # 確保 transcript_info 不是 None，才去讀取 location
        if transcript_info and transcript_info.get("location") == "drive":
            ep_match = re.search(r"(?i)EP\.?\s*(\d+)", base_name)
            if ep_match:
                drive_eps.add(int(ep_match.group(1)))

    # 分析情報，產生「下載黑名單」與「待上傳清單」
    completed_bases = set()
    completed_eps = set() 
    pending_uploads = []

    for base_name, info in status_report.items():
        transcript_info = info.get("transcript")
        
        if transcript_info:
            if FORCE_RETRANSCRIBE:
                pass 
            else:
                completed_bases.add(base_name)
                ep_match = re.search(r"(?i)EP\.?\s*(\d+)", base_name)
                if ep_match:
                    completed_eps.add(int(ep_match.group(1)))
            
            # 如果發現檔案「只存在本地」準備補傳...
            if transcript_info.get("location") == "local":
                ep_match = re.search(r"(?i)EP\.?\s*(\d+)", base_name)
                # 攔截！如果這集的數字已經存在於雲端 (不管是新舊檔名)，就放棄補傳！
                if ep_match and int(ep_match.group(1)) in drive_eps:
                    print(f"🛡️ 攔截上傳: {base_name} (雲端已存在相同集數)")
                    continue
                
                # 雲端真的沒有這集，才加入上傳清單
                pending_uploads.append(f"{base_name}.txt")
                pending_uploads.append(f"{base_name}.json")

    # --- 步驟一：下載最新音檔 ---
    print(f"\n>> [步驟 1/4]: 啟動 {SOURCE_TYPE.upper()} 下載器...")
    download_result = []
    
    if SOURCE_TYPE == "podcast":
        downloader = PodcastDownloader(RSS_URL, sub_dir=PODCAST_NAME)
        # 把情報局給的字串黑名單和 EP 數字黑名單一起傳給下載器
        download_result = downloader.download_recent_episodes(
            count=1,
            completed_bases=completed_bases, 
            completed_eps=completed_eps
        )
    elif SOURCE_TYPE == "youtube":
        yt_downloader = YouTubeDownloader(audio_dir)
        # 🌟 修改：傳遞 completed_bases。如果開啟強制重轉，就傳入空集合讓它乖乖重抓
        yt_blacklist = set() if FORCE_RETRANSCRIBE else completed_bases
        download_result = yt_downloader.download_audio(YOUTUBE_URL, completed_bases=yt_blacklist)
    else:
        print("❌ 未知的 SOURCE_TYPE 設定！")
        return

    # --- 步驟二：執行語音轉錄 ---
    print("\n>> [步驟 2/4]: 執行 Whisper 語音轉錄...")
    
    if not download_result:
        print("⏭️ 這次沒有需要轉錄的新檔案或指定集數。")
    else:
        is_colab = detect_environment()
        device = "cuda" if is_colab else "cpu"
        compute_type = "float16" if is_colab else "int8"
    
        transcriber = PodcastTranscriber(model_size=TARGET_MODEL, device=device, compute_type=compute_type)
        
        # 🌟 核心修改：不再掃描整個資料夾，而是精準針對這次指定的檔案名單進行轉錄
        for file_name in download_result:
            audio_path = os.path.join(audio_dir, file_name)
            transcriber.transcribe_file(
                audio_path=audio_path,
                output_dir=transcript_dir,
                language=TARGET_LANGUAGE,
                initial_prompt=INITIAL_PROMPT,
                force_retranscribe=FORCE_RETRANSCRIBE
            )

    # --- 步驟三：彙整待上傳清單 ---
    print("\n>> [步驟 3/4]: 盤點需要上傳至雲端的檔案...")
    # 除了情報局抓出的「卡在本地舊檔」，加上這次「剛剛才處理完的新集數」
    for file_name in download_result:
        base_name = os.path.splitext(file_name)[0]
        pending_uploads.append(f"{base_name}.txt")
        pending_uploads.append(f"{base_name}.json")

    # 剔除重複的檔名
    pending_uploads = list(set(pending_uploads))

    # --- 步驟四：上傳至雲端硬碟 ---
    print("\n>> [步驟 4/4]: 上傳結果至 Google Drive...")
    if pending_uploads:
        upload_files_to_drive(
            folder_id=DRIVE_FOLDER_ID, 
            target_dir=transcript_dir, 
            files_to_upload=pending_uploads,
            podcast_name=target_name
        )
    else:
        print("☁️ 雲端已同步至最新狀態，沒有新檔案需要上傳。")
        
    print("\n========== ✅ 自動化流程執行完畢！ ==========")

if __name__ == "__main__":
    main()