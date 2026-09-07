# Hugging Face Space, Docker SDK, cpu-basic (2 vCPU / 16 GB / ephemeral disk).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/user \
    APIX_OUT_DIR=/home/user/app/out

# HF Spaces runs as uid 1000. Writing anywhere else fails at runtime.
RUN useradd -m -u 1000 user
WORKDIR /home/user/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=user . .
RUN mkdir -p /home/user/app/out && chown -R user /home/user/app

USER user
# Spaces routes external traffic to 7860 only.
EXPOSE 7860
CMD ["uvicorn", "apix.app:app", "--host", "0.0.0.0", "--port", "7860", "--workers", "1"]
