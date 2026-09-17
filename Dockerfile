ARG PYTHON_BASE_IMAGE
FROM ${PYTHON_BASE_IMAGE} AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install from the fully pinned lock file, not from requirements.in.
COPY requirements.txt ./
# --require-hashes with no fallback. A fallback would silently install an
# unverified dependency set the moment the lock file lost its hashes, which is
# exactly the failure the hashes exist to prevent.
RUN pip install --require-hashes --no-deps -r requirements.txt

COPY policy_service ./policy_service
COPY migrations ./migrations
COPY schemas ./schemas
COPY prompts ./prompts

# Run as a non-root user.
RUN useradd --system --uid 10001 --no-create-home appuser
USER appuser

EXPOSE 8000
CMD ["uvicorn", "policy_service.main:app", "--host", "0.0.0.0", "--port", "8000"]