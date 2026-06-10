# 路跑號碼牌辨識與照片搜尋系統

上傳路跑賽事照片，系統自動辨識號碼牌，跑者輸入自己的號碼即可找到所有包含自己的照片。

---

## 功能特色

- **自動 OCR 辨識**：上傳後背景自動提取號碼牌，無需手動標記
- **多 OCR 引擎**：EasyOCR（GPU 加速）→ Tesseract → Claude Vision API → Google Cloud Vision
- **多號碼支援**：一張照片可辨識多位跑者的號碼
- **相簿管理**：依賽事建立相簿，支援個人上傳管理
- **用戶系統**：帳號/Email 登入，第一位用戶自動成為管理員
- **管理控制台**：網站標題、Logo、用戶管理、原廠重置
- **瀑布流展示**：搜尋結果響應式網格，支援點擊放大、鍵盤瀏覽
- **批次/拖曳上傳**：支援一次上傳多張，帶進度條
- **雲端儲存**：可選接 Cloudinary，節省本地磁碟
- **重新辨識**：對未辨識照片一鍵重跑 OCR

---

## 系統需求

- Python 3.9+
- [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki)（必要，Tesseract 引擎本體）

> EasyOCR、Claude Vision API、Google Cloud Vision 均為選配，見下方說明。

---

## 安裝與啟動

### 1. 安裝 Tesseract

```powershell
winget install UB-Mannheim.TesseractOCR
```

安裝後重新開啟終端機讓 PATH 生效。

### 2. 啟動系統

```powershell
cd "d:\Dev\影像辨識"
.\start.bat
```

`start.bat` 會自動建立虛擬環境、安裝套件、啟動伺服器。

### 3. 開啟瀏覽器

| 用途 | 網址 |
|------|------|
| 前端介面 | http://localhost:8000 |
| API 互動文件 | http://localhost:8000/docs |

---

## 手動安裝（不用 start.bat）

```powershell
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt
venv\Scripts\uvicorn.exe app.main:app --reload --host 0.0.0.0 --port 8000
```

---

## 專案結構

```
影像辨識/
├── app/                # 後端原始碼（Python 套件）
│   ├── main.py         # FastAPI 主程式（所有 API 路由、認證、相簿管理）
│   ├── ocr_engine.py   # OCR 核心（影像預處理 + EasyOCR / Tesseract / 雲端）
│   ├── database.py     # 資料庫連線設定
│   └── models.py       # ORM 資料模型
├── frontend/
│   └── index.html      # 前端介面（純 HTML/JS，單頁應用）
├── requirements.txt    # Python 套件清單
├── start.bat           # Windows 一鍵啟動
├── Dockerfile          # Docker 映像（含 Tesseract）
├── render.yaml         # Render.com 部署設定
├── .env.example        # 環境變數範本
├── static/             # 靜態資源（logo 等）
├── uploads/            # 上傳照片存放目錄（自動建立）
└── bib_recognition.db  # SQLite 資料庫（自動建立）
```

---

## API 端點

### 認證

| 方法 | 路徑 | 說明 |
|------|------|------|
| POST | `/api/auth/register` | 建立帳號 |
| POST | `/api/auth/login` | 登入（設定 HttpOnly Cookie） |
| POST | `/api/auth/logout` | 登出（清除 Cookie） |
| GET  | `/api/auth/me` | 取得目前登入者資訊 |
| PUT  | `/api/auth/me` | 更新個人資料 / 密碼 |

### 相簿

| 方法 | 路徑 | 說明 |
|------|------|------|
| GET    | `/api/albums` | 列出所有相簿（含照片數統計） |
| POST   | `/api/albums` | 建立相簿（需登入） |
| DELETE | `/api/albums/{id}` | 刪除相簿（需為擁有者或管理員） |
| GET    | `/api/albums/{id}/progress` | OCR 處理進度 |

### 照片

| 方法 | 路徑 | 說明 |
|------|------|------|
| POST   | `/api/upload` | 上傳照片（OCR 在背景執行） |
| GET    | `/api/photostream` | 分頁取得照片（可篩選相簿 / 未辨識） |
| DELETE | `/api/photos/{id}` | 刪除單張照片 |
| GET    | `/api/photos/{id}/download` | 下載原圖 |
| POST   | `/api/reprocess` | 重新辨識 |
| GET    | `/api/search?bib=&album_id=` | 依號碼搜尋照片 |

### 網站設定

| 方法 | 路徑 | 說明 |
|------|------|------|
| GET    | `/api/settings` | 取得網站設定 |
| PUT    | `/api/settings` | 更新網站標題（需管理員） |
| POST   | `/api/settings/logo` | 上傳 Logo（需管理員） |
| DELETE | `/api/settings/logo` | 移除 Logo（需管理員） |

### 管理

| 方法 | 路徑 | 說明 |
|------|------|------|
| GET    | `/api/admin/users` | 列出所有用戶（需管理員） |
| PUT    | `/api/admin/users/{id}` | 編輯用戶（需管理員） |
| DELETE | `/api/admin/users/{id}` | 刪除用戶（需管理員） |
| POST   | `/api/admin/factory-reset` | 原廠重置（需管理員 + 密碼確認） |
| GET    | `/api/stats` | 系統統計 |
| GET    | `/api/health` | 健康檢查（含 GPU 狀態） |

---

## OCR 精準度說明

### 本地引擎

**EasyOCR（主要，深度學習）**
- 自動偵測 CUDA，有 GPU 則加速
- 對每張圖進行全圖辨識 + 色彩分割 bib 區域辨識
- 低信心值再次放大區域進行二次辨識

**Tesseract（備援）**
- 生成 5 種預處理變體：Otsu 二值化 / 反轉 Otsu / 自適應門檻 / 形態學閉合 / 紅色轉黑遮罩
- 搭配 3 種 PSM 模式（11 / 6 / 3），取聯集過濾

### GPU 加速安裝（EasyOCR）

```powershell
# 先確認 CUDA 版本：nvidia-smi
# CUDA 12.x：
venv\Scripts\pip.exe install torch --index-url https://download.pytorch.org/whl/cu121
# CUDA 11.8：
venv\Scripts\pip.exe install torch --index-url https://download.pytorch.org/whl/cu118
# 再安裝 EasyOCR：
venv\Scripts\pip.exe install easyocr
```

### 雲端引擎（選配，最高精度）

**Claude Vision API：**
1. 安裝：`venv\Scripts\pip.exe install anthropic`
2. 設定：`$env:ANTHROPIC_API_KEY = "sk-ant-..."`
3. 呼叫：`extract_bib_numbers(path, use_claude_api=True)`

**Google Cloud Vision：**
1. 安裝：`venv\Scripts\pip.exe install google-cloud-vision`
2. 設定：`$env:GOOGLE_APPLICATION_CREDENTIALS = "C:\path\to\key.json"`
3. 呼叫：`extract_bib_numbers(path, use_vision_api=True)`

---

## 資料庫結構

```
users       → 用戶帳號（id, display_name, username, email, hashed_password, role, created_at）
albums      → 賽事相簿（id, name, event_date, description, created_at, owner_id→users）
photos      → 照片紀錄（id, filename, original_filename, upload_time, album_id→albums,
                        ocr_status, storage_url）
bib_numbers → 辨識結果（id, photo_id→photos, bib_number）
```

預設使用 SQLite（`bib_recognition.db`）。正式環境可改用 PostgreSQL，設定 `DATABASE_URL` 環境變數即可。

---

## 雲端部署（Render.com）

```powershell
# 安裝 Render CLI 後
render deploy
```

`render.yaml` 已設定：Docker Web Service + PostgreSQL + Cloudinary 環境變數。

---

## 技術棧

| 層級 | 技術 |
|------|------|
| 後端框架 | FastAPI |
| 影像處理 | OpenCV, Pillow |
| OCR 引擎 | EasyOCR / Tesseract / Claude Vision / Google Vision |
| 資料庫 ORM | SQLAlchemy |
| 資料庫 | SQLite / PostgreSQL |
| 雲端儲存 | Cloudinary（選配） |
| 認證 | JWT + HttpOnly Cookie + bcrypt |
| 前端 | HTML + Tailwind CSS + Vanilla JS |
