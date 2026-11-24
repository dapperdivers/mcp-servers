# Every server in this repository in one image; the container's argument picks which one
# runs (`args: [deals]`). The entrypoint is
#   python3 -m mcp_servers <name>
#
# Contract with the cluster (do not break):
#   - The argument is a server name from mcp_servers/__main__.py.
#   - Listens on 0.0.0.0:8000 (PORT overrides): MCP Streamable HTTP at /mcp, health at /healthz.
#   - MCP_AUTH_TOKEN is required; every /mcp request must send it as a bearer token.
#   - Runs as uid/gid 65534 with a read-only root filesystem; needs no writable path.
#   - /bin/sh exists, for a debugging shell (`command: ["/bin/sh"]` replaces the entrypoint).
#
# To move to a new Python: change the tag and the digest together, then let CI run the
# tests inside the image.
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

LABEL org.opencontainers.image.source="https://github.com/dapperdivers/mcp-servers" \
      org.opencontainers.image.description="Custom MCP tool servers for a homelab's AI agents"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --disable-pip-version-check --root-user-action=ignore \
        -r /tmp/requirements.txt \
 && rm -f /tmp/requirements.txt

WORKDIR /app
COPY mcp_servers/ /app/mcp_servers/

USER 65534:65534
EXPOSE 8000
ENTRYPOINT ["python3", "-m", "mcp_servers"]
CMD ["--help"]
