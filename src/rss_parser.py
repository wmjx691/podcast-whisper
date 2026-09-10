import feedparser
import os
import sys
import requests
import re
from tqdm import tqdm
from typing import List, Dict, Optional, Union, Set

from utils import get_project_root, detect_environment

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from episode_identity import EpisodeObservation


@dataclass(frozen=True)
class PublishedObservation:
    """Source time evidence attached to one observation, never part of identity."""

    observation: EpisodeObservation
    published_at: Optional[datetime] = None
    publication_text: Optional[str] = None

    def with_audio_path(self, path):
        return replace(self, observation=self.observation.with_audio_path(path))


def parse_published_observations(rss_content: bytes, *, feed_namespace: str):
    """Additive supplied-bytes API; ambiguous/zoneless pubDate fails closed.

    Association is by the same XML item order, never title, filename or URL.
    Download adapters must carry this wrapper through with_audio_path().
    """
    from xml.etree import ElementTree

    observations = parse_rss_observations(rss_content, feed_namespace=feed_namespace)
    items = ElementTree.fromstring(rss_content).findall("channel/item")
    result = []
    for observation, item in zip(observations, items):
        dates = item.findall("pubDate")
        raw = dates[0].text if len(dates) == 1 and len(dates[0]) == 0 else None
        published = None
        if raw:
            try:
                parsed = parsedate_to_datetime(raw)
                if parsed.utcoffset() is not None:
                    published = parsed.astimezone(timezone.utc)
            except (TypeError, ValueError, OverflowError):
                pass
        result.append(PublishedObservation(observation, published, raw))
    return result


def parse_rss_observations(rss_content: bytes, *, feed_namespace: str):
    """Opt-in RSS 2.0 observations from supplied bytes; never fetch or download.

    feedparser maps RSS GUID to entry.id (and sometimes link). Read the actual
    RSS element here so generic entry IDs/links cannot become GUID evidence,
    and preserve GUID text without feedparser's URI/whitespace normalization.
    Call observation.with_audio_path(...) to associate caller-supplied audio.
    """
    from xml.etree import ElementTree
    from episode_identity import EpisodeObservation, _require_text

    _require_text(feed_namespace, "feed_namespace")
    if not isinstance(rss_content, bytes):
        raise TypeError("rss_content must be supplied XML bytes, not a URL/path")
    root = ElementTree.fromstring(rss_content)
    if root.tag != "rss" or root.find("channel") is None:
        raise ValueError("explicit RSS channel/item evidence required")
    observations = []
    for item in root.findall("channel/item"):
        guids = item.findall("guid")
        guid = guids[0].text if len(guids) == 1 and len(guids[0]) == 0 else None
        if guid is not None and not guid.strip():
            guid = None
        enclosure = next((e.get("url") for e in item.findall("enclosure")
                          if e.get("type", "").startswith("audio")), None)
        observations.append(EpisodeObservation(
            feed_namespace=feed_namespace, source_guid=guid,
            title=item.findtext("title", ""), enclosure_url=enclosure,
        ))
    return observations


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

# --- 1. 核心下載器類別 ---
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

    # 🌟 新增：統一的檔名清理器，確保拿來比對的檔名跟實際存檔的檔名一模一樣
    def get_safe_basename(self, title: str) -> str:
        safe_title = title[:40] # 截斷標題避免過長
        return re.sub(r'[\\/*?:"<>|]', '', safe_title).strip()

    def download_file(self, url: str, filename: str) -> Optional[str]:
        # 這裡的 safe_filename 已經透過 get_safe_basename 處理過了
        file_path = os.path.join(self.save_dir, filename)

        if os.path.exists(file_path):
            print(f"⏭️  本地音檔已存在，跳過下載: {filename}")
            return file_path

        print(f"⬇️  開始下載: {filename}")
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

# 🌟 修改：加入 completed_eps 參數作為第二層過濾黑名單
    def download_specific_episodes(self, target_numbers: List[int], completed_bases: Set[str] = None, completed_eps: Set[int] = None):
        """下載指定集數"""
        if not self.episodes:
            self.parse_feed()
            
        completed_bases = completed_bases or set()
        completed_eps = completed_eps or set() # 🌟 新增：初始化 completed_eps
        print(f"\n🎯 準備下載指定集數: {target_numbers}")
        
        targets_set = set(target_numbers)
        downloaded_files = [] # 🌟 新增：紀錄成功下載或已存在的檔案，回傳給 main.py
        
        for ep in self.episodes:
            if ep['ep_number'] in targets_set:
                base_name = self.get_safe_basename(ep['title'])
                
                # 🌟 核心攔截邏輯：檔名完全一致，或是「EP集數」一致，就直接跳過！
                if base_name in completed_bases or (ep['ep_number'] is not None and ep['ep_number'] in completed_eps):
                    print(f"⏭️  情報顯示已完成轉錄，攔截下載: EP{ep['ep_number']} ({base_name})")
                    targets_set.remove(ep['ep_number'])
                    continue
                
                ext = ".mp3"
                if "m4a" in ep['url']: ext = ".m4a"
                filename = f"{base_name}{ext}"
                
                file_path = self.download_file(ep['url'], filename)
                if file_path:
                    downloaded_files.append(filename)
                targets_set.remove(ep['ep_number'])

        if targets_set:
            print(f"⚠️ 找不到以下集數 (可能未在 Feed 中或格式不符): {sorted(list(targets_set))}")
            
        return downloaded_files

# 🌟 修改：加入 completed_eps 參數作為第二層過濾
    def download_recent_episodes(self, count: int = 3, completed_bases: Set[str] = None, completed_eps: Set[int] = None):
        """下載最新 N 集"""
        if not self.episodes:
            self.parse_feed()
            
        completed_bases = completed_bases or set()
        completed_eps = completed_eps or set()
        print(f"\n🆕 準備檢查並下載最新 {count} 集")
        downloaded_files = []
        
        for ep in self.episodes[:count]:
            base_name = self.get_safe_basename(ep['title'])
            
            # 🌟 核心攔截邏輯：檔名完全一致，或是「EP集數」一致，就直接跳過！
            if base_name in completed_bases or (ep['ep_number'] is not None and ep['ep_number'] in completed_eps):
                print(f"⏭️  情報顯示已完成轉錄，攔截下載: EP{ep['ep_number']} ({base_name})")
                continue
                
            ext = ".mp3"
            if "m4a" in ep['url']: ext = ".m4a"
            filename = f"{base_name}{ext}"
            
            file_path = self.download_file(ep['url'], filename)
            if file_path:
                downloaded_files.append(filename)
                
        return downloaded_files

# --- 2. 使用者設定與執行區 ---
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
