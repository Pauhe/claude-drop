FROM python:3.13-slim

WORKDIR /app

# libheif is what pillow-heif needs at runtime to read iPhone photos.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libheif1 \
 && rm -rf /var/lib/apt/lists/*

COPY server/requirements.txt .
# pip and setuptools are build tooling; nothing at runtime imports them, and
# leaving them in was the whole of what the image scan still flagged (a path
# traversal in setuptools' PackageIndex, an out-of-bounds read in the msgpack
# pip vendors). A service that takes unauthenticated uploads has no business
# carrying a package installer.
RUN pip install --no-cache-dir -r requirements.txt \
 && pip uninstall -y pip setuptools

COPY server/app ./app
# Installer assets, served by the app so a new VM needs one command.
COPY install.sh ./install.sh
COPY cli/drop ./assets/drop
COPY skill/SKILL.md ./assets/SKILL.md

# Fixed uid so the host volume can be chowned to match.
# /data is created here, owned by that uid, so a fresh named volume inherits
# a writable directory instead of a root-owned one.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin drop \
 && mkdir /data && chown 10001:10001 /data
USER 10001

ENV DROP_ROOT=/data
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
