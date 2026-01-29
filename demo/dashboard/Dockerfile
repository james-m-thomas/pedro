FROM python:3.12-slim

RUN pip install --no-cache-dir \
    streamlit==1.41.* \
    duckdb==1.2.* \
    pyarrow==18.* \
    pandas==2.*

COPY app.py /app/app.py
WORKDIR /app

EXPOSE 8501

CMD ["streamlit", "run", "app.py", \
     "--server.address=0.0.0.0", \
     "--server.headless=true", \
     "--browser.gatherUsageStats=false"]
