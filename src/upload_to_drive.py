import os
import sys
import json
from typing import List, Optional
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from utils import get_project_root


# 開啟 Google Drive 全域讀寫權限
SCOPES = ['https://www.googleapis.com/auth/drive']

# --- 1. OAuth 2.0 驗證邏輯 ---
def get_oauth_credentials():
    project_root = get_project_root()
    token_path = os.path.join(project_root, 'token.json')
    client_secret_path = os.path.join(project_root, 'client_secret.json')
    
    creds = None
    # 如果之前已經登入過，就會有 token.json
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        
    # 如果沒有憑證，或是憑證過期了，就執行登入流程
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("🔄 憑證已過期，正在自動刷新...")
            creds.refresh(Request())
        else:
            print("🌐 準備開啟瀏覽器進行 Google 授權登入...")
            if not os.path.exists(client_secret_path):
                raise FileNotFoundError(f"❌ 找不到 {client_secret_path}，請確認是否已下載 OAuth 憑證！")
            flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, SCOPES)
            # 加入 open_browser=False，並指定一個 port (例如 0)，避免在某些環境下無法自動開啟瀏覽器的問題
            print("請複製下方的網址，貼到您電腦的瀏覽器中開啟並完成授權：")
            creds = flow.run_local_server(port=0, open_browser=False, bind_addr='127.0.0.1')
            
        # 把拿到的授權存進 token.json，下次就不用再開網頁登入了！
        with open(token_path, 'w') as token:
            token.write(creds.to_json())
            
    return creds

# --- 2. 尋找或自動建立雲端子資料夾 ---
def get_or_create_drive_subfolder(service, parent_id: str, folder_name: str) -> str:
    """在指定的父資料夾內尋找子資料夾，若無則自動建立。回傳子資料夾的 ID。"""
    query = f"'{parent_id}' in parents and name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    results = service.files().list(q=query, fields="files(id, name)").execute()
    items = results.get('files', [])
    
    if items:
        return items[0]['id'] # 找到了，直接回傳 ID
    
    # 找不到，自動幫使用者建立
    print(f"📁 雲端尚未有此節目的資料夾，正在自動建立子資料夾: {folder_name} ...")
    file_metadata = {
        'name': folder_name,
        'parents': [parent_id],
        'mimeType': 'application/vnd.google-apps.folder'
    }
    folder = service.files().create(body=file_metadata, fields='id').execute()
    return folder.get('id')

# --- 3. Google Drive 上傳邏輯 ---
# 由總司令 main.py 來指定要上傳哪些檔案
def upload_files_to_drive(folder_id: str, target_dir: str = None, files_to_upload: Optional[List[str]] = None, podcast_name: str = None):
    if target_dir is None:
        project_root = get_project_root()
        transcripts_dir = os.path.join(project_root, "data", "transcripts")
    else:
        transcripts_dir = target_dir
        
    if not os.path.exists(transcripts_dir):
        print(f"❌ 找不到轉錄檔資料夾: {transcripts_dir}")
        return

    print("🔑 正在驗證 OAuth 權限...")
    try:
        creds = get_oauth_credentials()
        service = build('drive', 'v3', credentials=creds)
    except Exception as e:
        print(f"❌ API 驗證失敗: {e}")
        return

    # 如果 main.py 沒有特別指定，就抓資料夾內所有的 .txt 和 .json
    if files_to_upload is None:
        actual_files = [f for f in os.listdir(transcripts_dir) if f.endswith(('.txt', '.json'))]
    else:
        actual_files = files_to_upload

    if not actual_files:
        print("⚠️ 沒有需要上傳的檔案。")
        return

    # 🌟 修改：根據 podcast_name 決定最終上傳的目標 ID
    upload_target_id = folder_id
    if podcast_name:
        upload_target_id = get_or_create_drive_subfolder(service, folder_id, podcast_name)

    print(f"📂 準備上傳 {len(actual_files)} 個檔案至 Google Drive 子資料夾...")

    # 🌟 新增：先取得雲端目標資料夾內的所有現存檔案，建立 {檔名: ID} 的對照表
    existing_files_map = {}
    query = f"'{upload_target_id}' in parents and trashed=false"
    try:
        # 抓取現存檔案清單
        results = service.files().list(q=query, fields="files(id, name)").execute()
        for item in results.get('files', []):
            existing_files_map[item['name']] = item['id']
    except Exception as e:
        print(f"⚠️ 無法取得雲端現存檔案清單，將全部視為新建: {e}")
    
    for filename in actual_files:
        filepath = os.path.join(transcripts_dir, filename)
        if not os.path.exists(filepath):
            print(f"⚠️ 找不到檔案，跳過上傳: {filename}")
            continue

        base_name = os.path.splitext(filename)[0]
        json_filepath = os.path.join(transcripts_dir, f"{base_name}.json")

        # --- 看本地 JSON 檔的 Metadata 準備做成雲端標籤 ---
        app_properties = {}
        if os.path.exists(json_filepath):
            try:
                with open(json_filepath, 'r', encoding='utf-8') as f:
                    metadata = json.load(f).get("metadata", {})
                    if metadata:
                        app_properties = {
                            "model_size": metadata.get("model_size", "unknown"),
                            "environment": metadata.get("environment", "unknown")
                        }
            except Exception:
                pass

        file_metadata = {'name': filename}
        
        # 將抽出來的 Metadata 塞進 Google Drive 的隱藏屬性 appProperties 裡面
        if app_properties:
            file_metadata['appProperties'] = app_properties

        mimetype = 'application/json' if filename.endswith('.json') else 'text/plain'
        media = MediaFileUpload(filepath, mimetype=mimetype, resumable=True)

        print(f"⬆️  正在同步: {filename} ...", end="", flush=True)
        try:
            # 🌟 核心防呆：判斷是要 Update 還是 Create
            if filename in existing_files_map:
                file_id = existing_files_map[filename]
                service.files().update(
                    fileId=file_id,
                    body=file_metadata, # 更新 Metadata
                    media_body=media    # 覆蓋實體檔案內容
                ).execute()
                print(f" ✅ 覆蓋成功 (雲端 ID: {file_id})")
            else:
                file_metadata['parents'] = [upload_target_id]
                uploaded_file = service.files().create(
                    body=file_metadata,
                    media_body=media,
                    fields='id'
                ).execute()
                print(f" ✅ 新建成功 (雲端 ID: {uploaded_file.get('id')})")
        except Exception as e:
            print(f" ❌ 失敗: {e}")