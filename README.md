# 路跑號碼牌辨識與照片搜尋系統

上傳路跑賽事照片，系統自動辨識號碼牌，跑者輸入自己的號碼即可找到所有包含自己的照片。

---

## 功能特色

- **自動 OCR 辨識**：上傳照片後自動提取號碼牌數字，無需手動標記
- **多號碼支援**：一張照片可辨識多位跑者的號碼
- **瀑布流展示**：搜尋結果以響應式網格呈現，支援點擊放大
- **批次上傳**：支援一次上傳多張照片
- **拖曳上傳**：支援拖曳檔案到上傳區域

---

## 系統需求

- Python 3.9+
- [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki)（必要，OCR 引擎本體）

---

## 安裝與啟動

### 1. 安裝 Tesseract

```powershell
winget install UB-Mannheim.TesseractOCR
```

安裝後重新開啟終端機讓 PATH 生效。若安裝路徑不在 PATH，請編輯 `ocr_engine.py` 第 14 行：

```python
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
```

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
venv\Scripts\uvicorn.exe main:app --reload --host 0.0.0.0 --port 8000
```

---

## 專案結構

```
影像辨識/
├── main.py             # FastAPI 主程式
├── ocr_engine.py       # OCR 核心（影像預處理 + Tesseract）
├── database.py         # 資料庫連線設定
├── models.py           # ORM 資料模型
├── requirements.txt    # Python 套件清單
├── start.bat           # Windows 一鍵啟動
├── .env.example        # 環境變數範本
├── frontend/
│   └── index.html      # 前端介面（純 HTML/JS）
├── uploads/            # 上傳照片存放目錄（自動建立）
└── bib_recognition.db  # SQLite 資料庫（自動建立）
```

---

## API 端點

### `POST /api/upload`

上傳一或多張照片，自動辨識號碼牌並存入資料庫。

**Request**: `multipart/form-data`，欄位名稱 `files`（可多個）

**Response**:
```json
{
  "uploaded": 2,
  "results": [
    {
      "original_filename": "race_photo_001.jpg",
      "stored_filename": "a1b2c3d4-....jpg",
      "photo_id": 1,
      "bib_numbers": ["1234", "5678"],
      "success": true
    }
  ]
}
```

### `GET /api/search?bib={number}`

查詢包含特定號碼的所有照片。

**Response**:
```json
{
  "bib": "1234",
  "count": 3,
  "photos": [
    {
      "photo_id": 1,
      "url": "/uploads/a1b2c3d4-....jpg",
      "original_filename": "race_photo_001.jpg",
      "upload_time": "2025-01-01T10:00:00"
    }
  ]
}
```

### `GET /api/stats`

```json
{ "total_photos": 150, "total_bib_records": 412 }
```

---

## OCR 精準度說明

系統對每張照片產生 4 種預處理變體（Otsu 二值化、反轉、自適應門檻、形態學閉合），搭配 3 種 Tesseract 辨識模式，取聯集後過濾非合理號碼，以提升遮擋或光線不均情況的辨識率。

若需更高精度，可改用 **Google Cloud Vision API**：

1. 安裝：`venv\Scripts\python.exe -m pip install google-cloud-vision`
2. 設定：`$env:GOOGLE_APPLICATION_CREDENTIALS = "C:\path\to\service-account.json"`
3. 程式碼中呼叫：`extract_bib_numbers(path, use_vision_api=True)`

---

## 資料庫結構

```
photos          → 每張照片一筆紀錄
bib_numbers     → 每個辨識到的號碼一筆紀錄（多對一關聯 photos）
```

預設使用 SQLite（`bib_recognition.db`）。正式環境可改用 PostgreSQL，僅需修改 `database.py` 中的 `DATABASE_URL`。

---

## 技術棧

| 層級 | 技術 |
|------|------|
| 後端框架 | FastAPI |
| 影像處理 | OpenCV, Pillow |
| OCR 引擎 | Tesseract (pytesseract) |
| 資料庫 ORM | SQLAlchemy |
| 資料庫 | SQLite / PostgreSQL |
| 前端 | HTML + Tailwind CSS + Vanilla JS |
