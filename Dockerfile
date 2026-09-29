# Multi-stage Dockerfile, to build and package an extraction plugin
#  Recommended way to build the plugin is by calling tox:
#    tox -e package
#  if you need to pass a proxy:
#    tox -e package -- --build-arg https_proxy=https://your-proxy
#  if you want to pass a private Python package index:
#     tox -e package -- --build-arg PIP_INDEX_URL=https://your-pypi-mirror
#  if the network intercepts TLS (pip: CERTIFICATE_VERIFY_FAILED), either pass its CA via a mirror/proxy,
#  or - INSECURE, packages are then not verified - skip certificate checks for PyPI and GitHub:
#     tox -e package -- --build-arg PIP_TRUSTED_HOST="pypi.org files.pythonhosted.org" --build-arg GIT_SSL_NO_VERIFY=1

###############################################################################
# Stage 1: build the plugin
# use a 'fat' image to setup the dependencies we'll need

FROM python:3.13 AS builder
ARG PIP_INDEX_URL=https://pypi.org/simple/
# opt-in only, unset by default (see above); pip and git read these as environment variables during RUN
ARG PIP_TRUSTED_HOST
ARG GIT_SSL_NO_VERIFY
RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"
COPY requirements.txt /requirements.txt
RUN pip install -Ur /requirements.txt

# ALEAPP runs in this same image, but in its own venv: it pins protobuf 5.x, the plugin SDK needs protobuf 7.x
# (git, present in this 'fat' image, is needed for ALEAPP's mister_skinnylegs dependency; lz4==4.3.3 of
# ccl_mozilla_reader has no Python 3.13 wheel and is compiled here)
RUN python -m venv /opt/aleapp-venv
COPY ALEAPP/requirements.txt /aleapp-requirements.txt
RUN /opt/aleapp-venv/bin/pip install -r /aleapp-requirements.txt


###############################################################################
# Stage 2: create the distributable plugin image
# use a 'slim' image for running the actual plugin

FROM python:3.13-slim
COPY --from=builder /venv /venv
COPY --from=builder /opt/aleapp-venv /opt/aleapp-venv
ENV PATH="/venv/bin:$PATH"

# plugin.py defaults: ALEAPP_DIR=/app/ALEAPP, ALEAPP_PYTHON=/opt/aleapp-venv/bin/python
COPY ALEAPP /app/ALEAPP
COPY plugin.py hleapp_rpc.py hleapp_launcher.py /app/
EXPOSE 8999
ENTRYPOINT ["serve_plugin", "-v"]
CMD ["/app/plugin.py", "8999"]
