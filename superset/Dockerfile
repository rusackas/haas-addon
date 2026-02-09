ARG BUILD_FROM=apache/superset:4.1.1
FROM ${BUILD_FROM}

# Install additional database drivers and dependencies
USER root

RUN pip install --no-cache-dir \
    pymysql \
    psycopg2-binary \
    && rm -rf /root/.cache

# Create directories for Home Assistant integration
RUN mkdir -p /share/superset /data /etc/superset

# Copy configuration and scripts
COPY rootfs /

# Make scripts executable
RUN chmod +x /etc/s6-overlay/s6-rc.d/*/up 2>/dev/null || true \
    && chmod +x /etc/s6-overlay/s6-rc.d/*/run 2>/dev/null || true \
    && chmod +x /usr/local/bin/* 2>/dev/null || true

# Set proper ownership
RUN chown -R superset:superset /share/superset /data /etc/superset

# Environment variables
ENV SUPERSET_CONFIG_PATH=/etc/superset/superset_config.py
ENV PYTHONPATH=/etc/superset:$PYTHONPATH
ENV FLASK_APP=superset

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD curl -f http://localhost:8088/health || exit 1

# Expose Superset port
EXPOSE 8088

# Use custom entrypoint that handles HA add-on lifecycle
# Run as root - HA add-ons are sandboxed and need root for volume setup
COPY rootfs/usr/local/bin/ha-entrypoint.sh /usr/local/bin/ha-entrypoint.sh
RUN chmod +x /usr/local/bin/ha-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/ha-entrypoint.sh"]
