FROM python:3.14-slim@sha256:a7fb1e634c4a578f9e0bd6327f11a3cde11b7a9395f48e24360c0988bcc5c2bc
WORKDIR /app
COPY --chown=root:root requirements.txt .
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.txt
COPY --chown=root:root app/ app/
COPY --chown=root:root bin/ bin/
COPY --chown=root:root public/ public/
RUN useradd --system --uid 10001 --user-group seance \
    && install -d -o 10001 -g 10001 -m 0700 /data \
    && chmod -R a-w /app
USER 10001:10001
EXPOSE 8000
CMD ["python", "bin/app.py"]
