FROM python:3.10-slim

RUN apt-get update && apt-get install -y \
    git curl wget vim sudo \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app/
RUN pip install --no-cache-dir -e .

CMD ["/bin/bash"]