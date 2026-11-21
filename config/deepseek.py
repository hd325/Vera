"""DeepSeek 官方 API：僅保留聯網搜尋讀取網頁正文（Fetch）。

聊天與所有後台大模型調用已改用華為雲 MaaS，見 config/llm.py。
"""

# Public Fetch endpoint only; discovery stays on Serper/Google.
DEEPSEEK_FETCH_API_URL = "https://api.deepseek.com/v1/tools/fetch"
DEEPSEEK_FETCH_DOCS_URL = "https://api-docs.deepseek.com/zh-cn/guides/search/"
