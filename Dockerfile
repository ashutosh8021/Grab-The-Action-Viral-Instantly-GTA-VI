FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY gtavi ./gtavi
RUN pip install --no-cache-dir .
COPY .streamlit ./.streamlit
EXPOSE 8501
CMD ["gtavi", "ui", "--server.address=0.0.0.0", "--server.port=8501"]
