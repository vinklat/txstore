#!/bin/sh
# Run the test suite on Debian's packaged dependencies → SETUP.md#dependency-policy.
# Every dependency comes from apt; pip installs only txstore itself, with no index.
set -eu

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
    python3 python3-venv python3-hatchling \
    python3-boto3 python3-botocore python3-requests \
    python3-pyxattr python3-moto python3-flask python3-flask-cors python3-responses \
    python3-pytest >/dev/null

python3 --version
python3 -m venv --system-site-packages /tmp/venv
/tmp/venv/bin/pip install --quiet --no-deps --no-index --no-build-isolation .
/tmp/venv/bin/python -m pytest -q -rs -p no:cacheprovider -o addopts=""
