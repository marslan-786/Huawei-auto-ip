# 1. Official Playwright Image (Is mein saari system libraries pehle se hain)
FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

# 2. Setup Work Directory
WORKDIR /app

# 3. Copy Requirements & Install Python Packages
COPY requirements.txt .
RUN pip install --no-cache-dir -U -r requirements.txt

# 4. Install Browsers (Sirf Chromium chahiye, baqi nahi)
RUN playwright install chromium

# 5. Copy Your Code
COPY . .

# 6. Create Captures Folder & Permissions (Yeh zaroori hai taake error na aye)
RUN mkdir -p captures && chmod 777 captures

# 7. Start Command (Jo aap ne di thi, wahi rakhi hai taake Port ka masla na ho)
CMD sh -c "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"