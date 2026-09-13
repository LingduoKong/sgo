#!/bin/sh
set -eu
for country in USA Japan India Singapore Brazil France; do
  docker run --rm --name sgo-dataset-download \
    --user 1000:1000 --memory 1100m --memory-swap 1100m --cpus 1 --pids-limit 128 \
    --read-only --cap-drop ALL --security-opt no-new-privileges \
    --tmpfs /tmp:size=128m,mode=1777 \
    -e OMP_NUM_THREADS=1 -e OPENBLAS_NUM_THREADS=1 \
    -v sgo_app_data:/app/data \
    -v /opt/sgo/deploy/download_datasets.py:/download_datasets.py:ro \
    --entrypoint python sgo-app -u /download_datasets.py "$country"
done
