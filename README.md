# Vera · 知行小智

面向學習與資料整理的 AI 助手：帶來源的全文檢索、多模型對話、照片與文件附件、練習任務，以及可選的文件編輯和微信接入。本專案由 HouKongIntellect 延續更新，公開倉庫名稱改為 **Vera**。

> 更新分支狀態：本分支為 Vera 遷移中的公開預覽。已檢查空密鑰啟動、資料隔離及敏感資訊；舊版接口的全量兼容測試和最新前端整合仍待完成，尚未合併為預設穩定版本。下方安裝方式用於本機試用，請以對應提交的檢查結果為準。

[下載 ZIP](https://github.com/hd325/Vera/archive/refs/heads/main.zip) · [安全說明](SECURITY.md) · [第三方授權](THIRD_PARTY_NOTICES.md)

## 功能與安裝範圍

| 功能 | 安裝與配置 |
| --- | --- |
| 本機帳號、對話、檔案管理與教師審核 | 核心安裝，建立自己的帳號 |
| 全文搜尋、段落來源及 TF-IDF 檢索 | 核心安裝，無需模型下載或外部密鑰 |
| AI 對話、文件分類、練習生成與評分 | 自配華為雲 MaaS 憑證 |
| 照片附件與看圖問答 | 使用供應商已開通、支援圖像的模型；掃描文字另可使用 OCR |
| 任務中心、暫停／繼續、練習作答與 PDF 匯出 | 任務記錄保存在本機；需要生成內容的步驟依賴模型接口 |
| 帳號設定與私人記憶 | 可查看、編輯、刪除記憶，並設定自動記憶及使用記憶 |
| 語音輸入與文字整理 | 需要瀏覽器語音識別支援及麥克風權限；文字整理使用模型接口 |
| 網頁搜尋與閱讀 | Serper 搜尋憑證，以及獨立的 DeepSeek Fetch 憑證／權限 |
| 圖片／掃描 PDF OCR | 自配華為雲 OCR 憑證；原生文字 PDF 可在本地提取 |
| 化學教師模式 | 包含通用教學規則；私人課堂證據由部署者自行取得授權 |
| Word／Excel／PowerPoint／PDF 建立與編輯 | 選裝文件工具，再啟用 `ENABLE_FILE_AGENT` |
| Office 轉 PDF 預覽 | 另裝 LibreOffice；缺少轉換器時仍可下載原檔 |
| 本機神經網絡嵌入 | 選裝模型依賴，並自行預先下載模型 |
| 微信接入 | 預設關閉；另裝固定版本的 Node.js／OpenClaw／通道插件並自行配置 |

公開版不包含真實帳號、密碼、密鑰、錄音、課堂轉寫、上傳原件、聊天記錄、微信憑證、模型權重或向量庫。示例與測試使用合成資料。

## 快速開始

建議使用 **Python 3.12**。核心安裝不需要 Node.js、PyTorch、本機大語言模型或任何外部密鑰。

下載 ZIP 並解壓，或使用 Git：

```sh
git clone https://github.com/hd325/Vera.git
cd Vera
```

Windows PowerShell：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe scripts/init_accounts.py --account my-admin --name "管理員" --role teacher --admin
.\.venv\Scripts\python.exe main.py
```

Linux／macOS：

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
.venv/bin/python scripts/init_accounts.py --account my-admin --name "管理員" --role teacher --admin
.venv/bin/python main.py
```

帳號工具在互動終端中隱藏密碼輸入，密碼至少 12 個字元，使用隨機鹽 PBKDF2 雜湊保存。開啟 **http://127.0.0.1:8000**，使用剛建立的帳號登入；沒有預設帳號或通用密碼。

密鑰留空時可以啟動網站及使用本機資料管理。AI 回答、生成、OCR 等外部功能需要完成相應配置，無法以空白密鑰取得真實結果。

建立學生帳號，例如：

```sh
python scripts/init_accounts.py --account student-01 --name "示例學生" --role student --grade "示例年級" --class-name "示例班"
```

以上命令請使用虛擬環境中的 Python。新增或修改帳號後重啟服務。教師角色以 `--role teacher` 指定，管理權限另外使用 `--admin`；帳號字首不決定權限。

## 配置外部服務

在自己的 `.env` 填入憑證，勿提交該檔案。程序環境變量優先；網站僅載入本專案根目錄的 `.env`。完整變量模板見 [.env.example](.env.example)。

| 配置 | 用途 |
| --- | --- |
| `MAAS_API_KEY` | 華為雲 MaaS 對話、分類、練習及其他模型功能 |
| `SERPER_API_KEY` | 網頁搜尋 |
| `DEEPSEEK_API_KEY` | DeepSeek Fetch 網頁閱讀；與 MaaS 聊天憑證分開配置 |
| `HUAWEI_AK`、`HUAWEI_SK` | 華為雲 OCR；必要時配置 `HUAWEI_PROJECT_ID`，區域見模板 |
| `CAPTCHA_APP_ID`、`CAPTCHA_APP_ID_SECRET`、`CAPTCHA_APP_KEY`、`CAPTCHA_APP_SECRET` | 可選滑塊驗證；未配置時不啟用，服務與憑證由部署者自行開通 |
| `EMBEDDING_BACKEND=tfidf` | 預設離線檢索，不下載神經模型 |
| `HOUKONG_HOST`、`HOUKONG_PORT` | 預設監聽 `127.0.0.1:8000` |
| `HOUKONG_DATA_DIR`、`HOUKONG_CHROMA_DIR` | 本機資料目錄與獨立向量庫目錄 |

模型選單與請求能力由 `config/llm.py` 定義，包括 DeepSeek-V4.1-Flash、GLM-5.3、openPangu-2.0-Flash。實際可用名稱、權限、思考及圖像能力由供應商決定；倉庫中的配置不代表部署者已開通全部模型。微信使用自己的模型配置，不跟隨網頁選單。

使用外部模型、OCR、網頁閱讀或驗證碼時，該操作需要的內容會交給對應服務處理。語音識別由瀏覽器提供；語音文字整理會把辨識文字送到模型接口。外部服務的費用、可用性與資料處理政策由部署者自行確認。

## 選裝功能

### 文件建立與編輯

```sh
python -m pip install -r requirements-file-agent.txt
```

在 `.env` 設定 `ENABLE_FILE_AGENT=true`，配置模型後重啟。此功能預設關閉；只安裝依賴不會自動開啟。文件工具透過服務端檔案 ID 取得授權資料，每個使用者／任務使用獨立目錄；輸出仍受原有檔案權限限制。

Office 原頁預覽另需 LibreOffice，讓 `soffice` 可由系統找到。生成的文件、公式、格式和資料仍應由使用者核對。

### 本機嵌入模型

```sh
python -m pip install -r requirements-local.txt
```

先自行下載模型，將 `LOCAL_EMBEDDING_MODEL` 指向本機模型目錄，再設定 `EMBEDDING_BACKEND=local`。執行時讀取既有模型；模型權重不隨倉庫提供。資源有限的主機可繼續使用 TF-IDF。

### 微信接入

`ENABLE_WECHAT=false` 為預設值。只使用網站時，不需要 Node.js、OpenClaw、微信登入或既有私人 profile。

倉庫保留接入源碼及固定版本的配置／適配腳本。這是需要單獨部署的集成，版本、憑證、通道登入與啟用條件見 [微信接入說明](integrations/openclaw-wechat/README.md)。本次公開版驗收不代表真實微信帳號、多帳號或文件收發已通過端到端驗證。

## 化學教師模式與私人資料

通用教學規則隨源碼提供。私人課堂證據由部署者自行取得授權；缺少證據時不應把通用知識說成某位老師的講課原話。

私人課堂資料放在 `HOUKONG_DATA_DIR/private/chemistry-classroom/`，其擁有者應對應自己的本機帳號。空白格式見 [合成模板](examples/chemistry-corpus.example.json)。自備並審核資料後可使用：

```sh
python scripts/build_chemistry_corpus.py --owner-account my-admin
```

這個工具整理部署者提供的本機資料，不會提供或連接任何人的錄音帳號、私人轉寫或同步服務。

## 資料、備份與部署

所有應用資料以 `HOUKONG_DATA_DIR` 為根目錄，預設為 `data/`。帳號、會話、對話、任務、私人記憶、已發布／個人文件、待審文件和集成狀態均屬運行資料，不應上傳到 GitHub。向量庫可用 `HOUKONG_CHROMA_DIR` 獨立指定；備份應同時涵蓋應用資料與實際向量庫位置。

學生提交的共享文件需要教師審核；個人文件只對擁有者可見。記憶與任務記錄保存在本機，但啟用 AI 操作時仍可能把相關授權內容發送到模型服務。

使用單一應用進程，避免多個寫入進程爭用同一資料目錄。對外部署需自行配置 HTTPS、反向代理、系統權限、請求限制、監控和備份。公開版的文件處理限制見 [SECURITY.md](SECURITY.md)；超限文件應先分拆。

伺服器可承載的人數取決於模型配額、回答時間、資料量、文件處理與部署配置。此倉庫不提供未經目標機實測的並發人數或延遲保證。

## 開發與測試

```sh
python -m pip install -r requirements-dev.txt -r requirements-file-agent.txt
python -m pip check
python -m unittest discover -s tests -v
```

在新的 Python 程序中執行測試。測試使用合成帳號、文件與模擬外部接口，並把資料及向量庫切換到臨時目錄；不需要真實 API 憑證、微信登入或模型權重。測試隔離與瀏覽器夾具說明見 [tests/README.md](tests/README.md)。

GitHub Actions 工作流檢查 Windows／Linux 的核心安裝和選裝依賴。實際通過狀態以對應提交的檢查結果為準；本機合成測試不能代替外部服務或正式伺服器驗收。

部分環境變量、API 標頭及集成 ID 保留 `HOUKONG` 名稱以維持兼容性；產品與公開倉庫名稱均為 Vera。
