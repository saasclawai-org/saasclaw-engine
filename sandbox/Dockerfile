FROM node:22-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    git make gcc g++ \
    jq curl \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g pnpm yarn 2>/dev/null || true

# Create non-root user for running commands
RUN useradd -m -u 1001 sandbox || true
USER sandbox
WORKDIR /workspace

CMD ["/bin/bash"]
