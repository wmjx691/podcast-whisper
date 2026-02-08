import feedparser
import os
import requests
from tqdm import tqdm
from typing import List, Dict, Optional
from datetime import datetime

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
        
        # 確保儲存目錄存在
        if not os.path.exists(self.save_dir):
            os.makedirs(self.save_dir)

    def parse_feed(self) -> List[Dict]:
            """
            解析 RSS Feed，回傳集數列表 (加入 User-Agent 偽裝)
            """
            print(f"正在解析 RSS: {self.rss_url} ...")
            
            # 1. 設定偽裝標頭 (讓 Server 覺得我們是瀏覽器，而不是機器人)
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            }

            try:
                # 2. 使用 requests 下載 XML 檔案
                response = requests.get(self.rss_url, headers=headers, timeout=15) # 設定 15秒超時
                response.raise_for_status() # 如果是 404 或 500 錯誤，這裡會直接報錯，跳到 except
                
                # 3. 將下載回來的內容 (response.content) 餵給 feedparser 解析
                # 注意：這裡解析的是「內容」，而不是「網址」
                self.feed = feedparser.parse(response.content)
                
            except Exception as e:
                # 捕捉網路連線、404、或被阻擋的錯誤
                raise ValueError(f"下載 RSS 失敗 (請檢查網址是否正確): {e}")

            # 4. 檢查 XML 格式是否異常 (但通常不影響讀取)
            if self.feed.bozo:
                print(f"警告: RSS 格式可能有誤，但嘗試繼續解析... ({self.feed.bozo_exception})")
            
            # 5. 取得頻道標題
            channel_title = self.feed.feed.get('title', 'Unknown')
            print(f"頻道名稱: {channel_title}")
            
            # 6. 提取集數
            episodes = []
            for entry in self.feed.entries:
                audio_url = None
                # 尋找音訊連結
                for link in entry.get('links', []):
                    if link.get('type', '').startswith('audio'):
                        audio_url = link.get('href')
                        break
                
                # 有些 RSS 的音訊連結是在 'enclosures' 裡面，這裡做個雙重保險
                if not audio_url and 'enclosures' in entry:
                    for enclosure in entry.enclosures:
                        if enclosure.get('type', '').startswith('audio'):
                            audio_url = enclosure.get('href')
                            break

                if audio_url:
                    episodes.append({
                        'title': entry.get('title', 'No Title'),
                        'date': entry.get('published', ''),
                        'url': audio_url
                    })
            
            print(f"共找到 {len(episodes)} 集節目。")
            return episodes

    def download_episode(self, episode_url: str, filename: str) -> Optional[str]:
        """
        下載單集音訊
        :param episode_url: 音訊網址
        :param filename: 儲存檔名 (包含副檔名)
        :return: 下載後的完整路徑，失敗則回傳 None
        """
        # 清理檔名中的非法字元
        safe_filename = "".join([c for c in filename if c.isalpha() or c.isdigit() or c in (' ', '-', '_', '.')]).rstrip()
        file_path = os.path.join(self.save_dir, safe_filename)

        # 如果檔案已存在，跳過下載
        if os.path.exists(file_path):
            print(f"檔案已存在，跳過: {safe_filename}")
            return file_path

        print(f"開始下載: {safe_filename}")
        try:
            response = requests.get(episode_url, stream=True)
            response.raise_for_status()
            
            total_size = int(response.headers.get('content-length', 0))
            
            with open(file_path, 'wb') as f, tqdm(
                desc=safe_filename,
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
            print(f"下載失敗: {e}")
            return None


# --- 測試區 (當直接執行此檔案時會跑這段) ---
if __name__ == "__main__":
    # 選項 A: 呱吉 (Gua Ji) - 已更新為最新 ID
    # 來源參考: SoundOn Player ID (ecd31076-d12d-46dc-ba11-32d24b41cca5)
    TEST_RSS = "https://feeds.soundon.fm/podcasts/ecd31076-d12d-46dc-ba11-32d24b41cca5.xml"
    
    # 選項 B: 百靈果 News (Bailingguo News) - 備用測試
    # TEST_RSS = "https://feeds.soundon.fm/podcasts/d316b355-aaa0-4632-b0e0-27188805aa04.xml"
        
    downloader = PodcastDownloader(TEST_RSS)
    ep_list = downloader.parse_feed()
    
    # 試著下載最新的一集來測試
    if ep_list:
        # 為了避免下載太久，我們只印出資訊，或下載前先問使用者
        latest_ep = ep_list[0]
        print(f"準備下載最新一集: {latest_ep['title']}")
        
        # 這裡會真的開始下載 (您可以把這行註解掉，如果你只是想測解析功能)
        downloader.download_episode(latest_ep['url'], f"{latest_ep['title']}.mp3")