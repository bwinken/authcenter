FROM python:3.12-slim

# --- ODBC Driver 17 (離線安裝) ---
# 將預先下載的 .deb 檔案放在 odbc-offline/ 目錄
# 如果該目錄不存在則跳過（適用於不需要 MSSQL 的環境）
COPY odbc-offline/ /tmp/odbc-offline/
RUN if ls /tmp/odbc-offline/*.deb >/dev/null 2>&1; then \
        dpkg -i /tmp/odbc-offline/unixodbc-common_*.deb \
                /tmp/odbc-offline/libltdl7_*.deb && \
        dpkg -i /tmp/odbc-offline/libodbc2_*.deb \
                /tmp/odbc-offline/libodbcinst2_*.deb \
                /tmp/odbc-offline/libodbccr2_*.deb && \
        dpkg -i /tmp/odbc-offline/libodbc1_*.deb \
                /tmp/odbc-offline/odbcinst_*.deb \
                /tmp/odbc-offline/unixodbc_*.deb && \
        ACCEPT_EULA=Y dpkg -i /tmp/odbc-offline/msodbcsql17_*.deb && \
        rm -rf /tmp/odbc-offline; \
    fi

WORKDIR /app

# --- Python 依賴 ---
# 內網需透過 Proxy 時，build 時傳入：
#   docker compose build --build-arg http_proxy=http://proxy:8080 --build-arg https_proxy=http://proxy:8080
ARG http_proxy
ARG https_proxy
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- 應用程式碼 ---
COPY app/ app/
COPY generate_keys.py .
COPY config/apps.yaml.example config/apps.yaml.example

RUN mkdir -p keys config

EXPOSE 8000

CMD ["fastapi", "run", "app/main.py", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
