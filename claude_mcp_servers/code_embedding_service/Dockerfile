FROM docker.io/pytorch/pytorch:2.6.0-cuda12.6-cudnn9-runtime

WORKDIR /app

# Install Python dependencies (sentence-transformers pulls torch but base image has it)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

# Redirect ALL HuggingFace state (model weights + modules cache) to mounted volume.
# This ensures custom model code (trust_remote_code=True) is also cached persistently.
ENV HF_HOME=/cache

EXPOSE 11440

CMD ["python", "server.py"]
