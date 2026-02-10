import feedparser
import os
import requests
import re  # 新增：用於正規表達式
from tqdm import tqdm
from typing import List, Dict, Optional, Union

class PodcastDownloader:
    def __init__(self, rss_url: str, save_dir: str = "data/audio"):
        """
        初始化 Podcast 下載器
        :param rss_url: Podcast 的 RSS Feed 網址
        :param save_dir: 檔案儲存路徑
        """
        self.rss_url = rss_url
        self.save_dir = save_dir
        self.feed = None
        self.episodes = [] # 儲存解析後的集數列表
        
        # 確保儲存目錄存在
        if not os.path.exists(self.save_dir):
            os.makedirs(self.save_dir)

    def parse_feed(self) -> List[Dict]:
        """
        解析 RSS Feed，回傳集數列表 (加入 User-Agent 偽裝)
        """
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

        if self.feed.bozo:
            print(f"⚠️ 警告: RSS 格式可能有誤 ({self.feed.bozo_exception})")
        
        channel_title = self.feed.feed.get('title', 'Unknown')
        print(f"✅ 頻道名稱: {channel_title}")
        
        self.episodes = [] # 重置列表
        for entry in self.feed.entries:
            audio_url = None
            # 優先從 links 找
            for link in entry.get('links', []):
                if link.get('type', '').startswith('audio'):
                    audio_url = link.get('href')
                    break
            
            # 備用方案：從 enclosures 找
            if not audio_url and 'enclosures' in entry:
                for enclosure in entry.enclosures:
                    if enclosure.get('type', '').startswith('audio'):
                        audio_url = enclosure.get('href')
                        break

            if audio_url:
                title = entry.get('title', 'No Title')
                # --- 新增功能：嘗試提取集數號碼 ---
                # 使用 Regex 尋找 "EP" 後面的數字，例如 "EP418", "EP 418", "ep418"
                # (?i) 代表忽略大小寫
                ep_match = re.search(r"(?i)EP\s*(\d+)", title)
                ep_number = int(ep_match.group(1)) if ep_match else None

                self.episodes.append({
                    'title': title,
                    'ep_number': ep_number, # 儲存提取出的集數 (int)
                    'date': entry.get('published', ''),
                    'url': audio_url
                })
        
        print(f"📊 共找到 {len(self.episodes)} 集節目。")
        return self.episodes

    def download_episode(self, episode_url: str, filename: str) -> Optional[str]:
        """
        下載單集音訊
        """
        # 清理檔名 (移除特殊符號，只保留中英數字與底線)
        safe_filename = re.sub(r'[\\/*?:"<>|]', '', filename).strip()
        file_path = os.path.join(self.save_dir, safe_filename)

        if os.path.exists(file_path):
            print(f"⏭️  檔案已存在，跳過: {safe_filename}")
            return file_path

        print(f"⬇️  開始下載: {safe_filename}")
        try:
            response = requests.get(episode_url, stream=True)
            response.raise_for_status()
            
            total_size = int(response.headers.get('content-length', 0))
            
            with open(file_path, 'wb') as f, tqdm(
                desc="Progress",
                total=total_size,
                unit='iB',
                unit_scale=True,
                unit_divisor=1024,
            ) as bar:
                for data in response.iter_content(chunk_size=1024):
                    size = f.write(data)
                    bar.update(size)
            
            return file_path
        except Exception as e:
            print(f"❌ 下載失敗: {e}")
            return None

    def download_specific_episodes(self, target_numbers: List[int]):
        """
        批次下載指定的集數列表
        :param target_numbers: 要下載的集數列表，例如 [418, 414, 408]
        """
        if not self.episodes:
            self.parse_feed()

        print(f"\n🎯 準備下載指定集數: {target_numbers}")
        
        # 轉換成 Set 加速搜尋
        targets_set = set(target_numbers)
        found_count = 0

        for ep in self.episodes:
            if ep['ep_number'] in targets_set:
                # 檔名範例: "EP418_2026年房市租賃市場.mp3"
                # 這裡我們把標題稍微縮短一點，避免檔名太長
                safe_title = ep['title'][:50] # 取前50個字
                filename = f"{safe_title}.mp3"
                
                self.download_episode(ep['url'], filename)
                found_count += 1
                
                # 從待下載清單中移除 (避免重複處理)
                targets_set.remove(ep['ep_number'])

        if targets_set:
            print(f"\n⚠️ 以下集數未在 RSS 中找到 (可能太舊或標題格式不同): {sorted(list(targets_set))}")
        else:
            print(f"\n✨ 所有指定集數下載完成！")

# --- 測試區 ---
if __name__ == "__main__":
    # 歐本豪斯 Open House RSS
    RSS_URL = "https://feed.firstory.me/rss/user/cke0tqspfvlc00803lwhmdb2t"
    
    downloader = PodcastDownloader(RSS_URL, save_dir="data/audio/openhouse")
    
    # === 使用者設定區 ===
    # 方式 A: 指定特定集數 (您的需求)
    TARGET_EPS = [418, 414, 408, 396, 392]
    
    # 方式 B: 如果想要下載區間 (例如 400 到 405)，可以把下面註解打開
    # TARGET_EPS = list(range(400, 406)) 
    
    # 執行下載
    downloader.download_specific_episodes(TARGET_EPS)