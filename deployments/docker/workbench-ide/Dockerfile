# Workbench IDE image: code-server + owner-supplied Menagerie VSIX.
# Build (context = this directory, VSIX copied into it):
#   docker build --build-arg MENAGERIE_VSIX=menagerie.vsix --build-arg MENAGERIE_VERSION=1.2.3 .
# No provider API key and no git credentials are baked in (Req 8.4); the pod supplies the
# OmniRoute key at runtime.
ARG CODE_SERVER_VERSION=4.105.1
FROM codercom/code-server:${CODE_SERVER_VERSION}

ARG MENAGERIE_VSIX
ARG MENAGERIE_VERSION
RUN test -n "${MENAGERIE_VSIX}" || { echo "build arg MENAGERIE_VSIX is required" >&2; exit 1; }
RUN test -n "${MENAGERIE_VERSION}" || { echo "build arg MENAGERIE_VERSION is required" >&2; exit 1; }

USER root
RUN mkdir -p /opt/menagerie/extensions /tmp/menagerie \
    && chown -R 1000:1000 /opt/menagerie /tmp/menagerie

# VSIX comes from the build context; the build fails if the file is absent.
COPY ${MENAGERIE_VSIX} /tmp/menagerie.vsix
RUN code-server --extensions-dir /opt/menagerie/extensions --install-extension /tmp/menagerie.vsix \
    && rm -f /tmp/menagerie.vsix \
    && chown -R 1000:1000 /opt/menagerie/extensions \
    && echo "${MENAGERIE_VERSION}" > /opt/menagerie/VERSION

# git wrapper first on PATH; real git stays at /usr/bin/git.
COPY git-wrapper.sh /usr/local/bin/git
COPY menagerie-status.js /opt/menagerie/menagerie-status.js
COPY entrypoint.sh /opt/menagerie/entrypoint.sh
RUN chmod 0755 /usr/local/bin/git /opt/menagerie/entrypoint.sh \
    && test -x /usr/bin/git

ENV PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin \
    ROO_CODE_IPC_SOCKET_PATH=/tmp/menagerie/ipc.sock \
    MENAGERIE_STATUS_PORT=8081 \
    WORKBENCH_PUSH_APPROVED=0

USER 1000
EXPOSE 8080 8081
ENTRYPOINT ["/opt/menagerie/entrypoint.sh"]
