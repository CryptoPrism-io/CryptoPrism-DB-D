# CP-011 Stage 2B worker image (built from branch tip, immutable tag = commit sha)
# Build context: aws/cp011-stage2b/  (contains app/, requirements.txt, run_all.sh,
# pick_schema.py, probe_rds.py, s3_upload.py)
# python 3.13 + pinned pandas 2.3.1/numpy 2.3.2 match methodology_v2_freeze.json;
# the frozen gcp_dmv_osc functions require pandas 2.3.1 groupby.apply behavior.
FROM python:3.13-slim

WORKDIR /app

# requirements first for layer caching
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# the validated pit package + TA modules + worker driver
COPY app/ /app/app/
COPY run_all.sh pick_schema.py probe_rds.py s3_upload.py /app/

ENV GITHUB_ACTIONS=1
ENTRYPOINT ["bash", "/app/run_all.sh"]
