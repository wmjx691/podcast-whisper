import feedparser
import os
import sys
import requests
import re
from tqdm import tqdm
from typing import List, Dict, Optional, Union

# --- 1. 環境設定區 (與 transcriber.py 共用邏輯) ---
def detect_environment():
    """偵測是否在 Colab 環境"""
    return "COLAB_RELEASE_TAG" in os.environ or 'google.colab' in sys.modules

def get_project_root():
    """回傳專案根目錄"""
    if detect_environment():
        # Colab 路徑
        root = '/content/drive/MyDrive/MyProject/whisper'
        # 簡單檢查掛載
        if not os.path.exists('/content/drive'):
            print("⚠️ Colab 環境但未檢測到 Drive，請確保已執行 drive.mount()")
        return root
    else:
        # 本地路徑
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --- 2. 核心下載器類別 ---
class PodcastDownloader:
    def __init__(self, rss_url: str, sub_dir: str = "downloads"):
        """
        初始化 Podcast 下載器
        :param rss_url: Podcast 的 RSS Feed 網址
        :param sub_dir: 儲存子資料夾名稱 (例如: "openhouse" 或 "gooaye")
        """
        self.rss_url = rss_url
        self.episodes = [] # 儲存解析後的集數列表
        
        # 自動決定儲存路徑
        project_root = get_project_root()
        self.save_dir = os.path.join(project_root, "data", "audio", sub_dir)
        
        # 確保目錄存在
        if not os.path.exists(self.save_dir):
            os.makedirs(self.save_dir)
            print(f"📂 建立目錄: {self.save_dir}")
        else:
            print(f"📂 下載目錄: {self.save_dir}")

    def parse_feed(self) -> List[Dict]:
        """解析 RSS Feed 並提取集數資訊"""
        print(f"📡 正在解析 RSS: {self.rss_url} ...")
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }

        try:
            response = requests.get(self.rss_url, headers=headers, timeout=15)
            response.raise_for_status()
            self.feed = feedparser.parse(response.content)
        except Exception as e:
            raise ValueError(f"❌ 下載 RSS 失敗: {e}")

        channel_title = self.feed.feed.get('title', 'Unknown')
        print(f"✅ 頻道名稱: {channel_title}")
        
        self.episodes = [] # 重置列表
        for entry in self.feed.entries:
            audio_url = None
            # 優先從 links 找 audio 類型
            for link in entry.get('links', []):
                if link.get('type', '').startswith('audio'):
                    audio_url = link.get('href')
                    break
            
            # 備用：從 enclosures 找
            if not audio_url and 'enclosures' in entry:
                for enclosure in entry.enclosures:
                    if enclosure.get('type', '').startswith('audio'):
                        audio_url = enclosure.get('href')
                        break

            if audio_url:
                title = entry.get('title', 'No Title')
                
                # --- Regex 提取集數 ---
                # 支援: EP418, ep 418, Ep.418
                ep_match = re.search(r"(?i)EP\.?\s*(\d+)", title)
                ep_number = int(ep_match.group(1)) if ep_match else None

                self.episodes.append({
                    'title': title,
                    'ep_number': ep_number,
                    'date': entry.get('published', ''),
                    'url': audio_url
                })
        
        print(f"📊 共找到 {len(self.episodes)} 集節目。")
        return self.episodes

    def download_file(self, url: str, filename: str) -> Optional[str]:
        """下載單一檔案 (含進度條)"""
        # 清理檔名非法字元
        safe_filename = re.sub(r'[\\/*?:"<>|]', '', filename).strip()
        file_path = os.path.join(self.save_dir, safe_filename)

        if os.path.exists(file_path):
            print(f"⏭️  檔案已存在，跳過: {safe_filename}")
            return file_path

        print(f"⬇️  開始下載: {safe_filename}")
        try:
            response = requests.get(url, stream=True)
            response.raise_for_status()
            total_size = int(response.headers.get('content-length', 0))
            
            # 使用 tqdm 顯示進度
            with open(file_path, 'wb') as f, tqdm(
                total=total_size, unit='iB', unit_scale=True, unit_divisor=1024, 
                desc="Progress", leave=False
            ) as bar:
                for data in response.iter_content(chunk_size=1024):
                    size = f.write(data)
                    bar.update(size)
            
            print(f"   ✅ 下載完成")
            return file_path
        except Exception as e:
            print(f"❌ 下載失敗: {e}")
            if os.path.exists(file_path):
                os.remove(file_path) # 下載失敗則刪除殘檔
            return None

    def download_specific_episodes(self, target_numbers: List[int]):
        """下載指定集數 (您要的功能)"""
        if not self.episodes:
            self.parse_feed()

        print(f"\n🎯 準備下載指定集數: {target_numbers}")
        
        # 轉換成 Set 加速搜尋
        targets_set = set(target_numbers)
        
        for ep in self.episodes:
            if ep['ep_number'] in targets_set:
                # 檔名範例: EP418_標題.mp3
                # 取得副檔名
                ext = ".mp3"
                if "m4a" in ep['url']: ext = ".m4a"
                
                safe_title = ep['title'][:40] # 截斷標題避免過長
                filename = f"{safe_title}{ext}"
                
                self.download_file(ep['url'], filename)
                targets_set.remove(ep['ep_number'])

        if targets_set:
            print(f"⚠️ 找不到以下集數 (可能未在 Feed 中或格式不符): {sorted(list(targets_set))}")

    def download_recent_episodes(self, count: int = 3):
        """下載最新 N 集 (Colab 測試方便用)"""
        if not self.episodes:
            self.parse_feed()
            
        print(f"\n🆕 準備下載最新 {count} 集")
        for ep in self.episodes[:count]:
            ext = ".mp3"
            if "m4a" in ep['url']: ext = ".m4a"
            safe_title = ep['title'][:40]
            filename = f"{safe_title}{ext}"
            self.download_file(ep['url'], filename)

# --- 3. 使用者設定與執行區 ---
if __name__ == "__main__":
    
    # 範例 RSS (Open House 歐本豪斯)
    RSS_URL = "https://feed.firstory.me/rss/user/cke0tqspfvlc00803lwhmdb2t"
    
    # 建立下載器 (會自動存到 data/audio/openhouse)
    downloader = PodcastDownloader(RSS_URL, sub_dir="openhouse")
    
    # # === [模式 A] 指定集數下載 (還原您的需求) ===
    # # 填入您想下載的集數號碼
    # TARGET_EPS = [418, 414, 408, 396, 392]
    # downloader.download_specific_episodes(TARGET_EPS)
    
    # # === [模式 B] 下載區間 (例如 400 到 405) (選用) ===
    # TARGET_EPS = list(range(400, 406)) 
    # downloader.download_specific_episodes(TARGET_EPS)

    # # === [模式 C] 下載最新集數 (選用) ===
    # # 如果不想指定，只想抓最新的，把下面註解打開
    downloader.download_recent_episodes(3)
