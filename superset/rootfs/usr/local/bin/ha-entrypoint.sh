#!/bin/bash
set -e

# Apache Superset Add-on Entrypoint
# This script initializes Superset for Home Assistant and starts the web server

echo "================================================"
echo "Apache Superset for Home Assistant"
echo "================================================"

# Paths
SHARE_DIR="/share/superset"
DATA_DIR="/data"
OPTIONS_FILE="/data/options.json"
INIT_FLAG="${SHARE_DIR}/.initialized"
DASHBOARDS_FLAG="${SHARE_DIR}/.dashboards_imported"
SECRET_KEY_FILE="${SHARE_DIR}/.secret_key"

# Create necessary directories
mkdir -p "${SHARE_DIR}" "${DATA_DIR}"

# Parse options from Home Assistant
if [ -f "${OPTIONS_FILE}" ]; then
    echo "Reading configuration from Home Assistant..."
    DATABASE_TYPE=$(jq -r '.database_type // "sqlite"' "${OPTIONS_FILE}")
    DATABASE_HOST=$(jq -r '.database_host // ""' "${OPTIONS_FILE}")
    DATABASE_PORT=$(jq -r '.database_port // 3306' "${OPTIONS_FILE}")
    DATABASE_NAME=$(jq -r '.database_name // "homeassistant"' "${OPTIONS_FILE}")
    DATABASE_USER=$(jq -r '.database_user // ""' "${OPTIONS_FILE}")
    DATABASE_PASSWORD=$(jq -r '.database_password // ""' "${OPTIONS_FILE}")
    SUPERSET_SECRET_KEY=$(jq -r '.superset_secret_key // ""' "${OPTIONS_FILE}")
    ADMIN_PASSWORD=$(jq -r '.admin_password // ""' "${OPTIONS_FILE}")
else
    echo "No options file found, using defaults..."
    DATABASE_TYPE="sqlite"
    DATABASE_HOST=""
    DATABASE_PORT=3306
    DATABASE_NAME="homeassistant"
    DATABASE_USER=""
    DATABASE_PASSWORD=""
    SUPERSET_SECRET_KEY=""
    ADMIN_PASSWORD=""
fi

# Generate or retrieve secret key
if [ -z "${SUPERSET_SECRET_KEY}" ]; then
    if [ -f "${SECRET_KEY_FILE}" ]; then
        SUPERSET_SECRET_KEY=$(cat "${SECRET_KEY_FILE}")
        echo "Using existing secret key"
    else
        SUPERSET_SECRET_KEY=$(openssl rand -base64 42)
        echo "${SUPERSET_SECRET_KEY}" > "${SECRET_KEY_FILE}"
        chmod 600 "${SECRET_KEY_FILE}"
        echo "Generated new secret key"
    fi
fi

# Generate or retrieve admin password
ADMIN_PASSWORD_FILE="${SHARE_DIR}/.admin_password"
if [ -z "${ADMIN_PASSWORD}" ]; then
    if [ -f "${ADMIN_PASSWORD_FILE}" ]; then
        ADMIN_PASSWORD=$(cat "${ADMIN_PASSWORD_FILE}")
        echo "Using existing admin password"
    else
        ADMIN_PASSWORD=$(openssl rand -base64 12)
        echo "${ADMIN_PASSWORD}" > "${ADMIN_PASSWORD_FILE}"
        chmod 600 "${ADMIN_PASSWORD_FILE}"
        echo "================================================"
        echo "Generated admin password: ${ADMIN_PASSWORD}"
        echo "Please save this password!"
        echo "================================================"
    fi
else
    # User provided password - save it
    echo "${ADMIN_PASSWORD}" > "${ADMIN_PASSWORD_FILE}"
    chmod 600 "${ADMIN_PASSWORD_FILE}"
fi

# Build Home Assistant Recorder database URI
case "${DATABASE_TYPE}" in
    sqlite)
        HA_DATABASE_URI="sqlite:////config/home-assistant_v2.db"
        echo "Using SQLite database at /config/home-assistant_v2.db"
        ;;
    mysql)
        HA_DATABASE_URI="mysql+pymysql://${DATABASE_USER}:${DATABASE_PASSWORD}@${DATABASE_HOST}:${DATABASE_PORT}/${DATABASE_NAME}"
        echo "Using MySQL database at ${DATABASE_HOST}:${DATABASE_PORT}/${DATABASE_NAME}"
        ;;
    postgresql)
        HA_DATABASE_URI="postgresql+psycopg2://${DATABASE_USER}:${DATABASE_PASSWORD}@${DATABASE_HOST}:${DATABASE_PORT}/${DATABASE_NAME}"
        echo "Using PostgreSQL database at ${DATABASE_HOST}:${DATABASE_PORT}/${DATABASE_NAME}"
        ;;
    *)
        echo "Unknown database type: ${DATABASE_TYPE}, defaulting to SQLite"
        HA_DATABASE_URI="sqlite:////config/home-assistant_v2.db"
        ;;
esac

# Export environment variables for Superset config
export SUPERSET_SECRET_KEY="${SUPERSET_SECRET_KEY}"
export HA_DATABASE_URI="${HA_DATABASE_URI}"
export HA_DATABASE_NAME="${DATABASE_NAME}"

# Generate dynamic Superset configuration
cat > /etc/superset/superset_config.py << EOF
import os

# Security
SECRET_KEY = os.environ.get("SUPERSET_SECRET_KEY", "CHANGE_ME")

# Superset's own metadata database
SQLALCHEMY_DATABASE_URI = "sqlite:////share/superset/superset.db"

# Web server configuration
SUPERSET_WEBSERVER_PORT = 8088
SUPERSET_WEBSERVER_TIMEOUT = 120
ENABLE_PROXY_FIX = True

# Worker configuration (low memory footprint)
SUPERSET_WORKERS = 2

# Disable CSRF for ingress (HA handles auth)
WTF_CSRF_ENABLED = False

# Feature flags - disable heavy features
FEATURE_FLAGS = {
    "ALERT_REPORTS": False,
    "ENABLE_TEMPLATE_PROCESSING": True,
    "EMBEDDED_SUPERSET": False,
    "THUMBNAILS": False,
    "SCHEDULED_QUERIES": False,
}

# Simple caching (no Redis required)
CACHE_CONFIG = {
    "CACHE_TYPE": "SimpleCache",
    "CACHE_DEFAULT_TIMEOUT": 300,
}
DATA_CACHE_CONFIG = {
    "CACHE_TYPE": "SimpleCache",
    "CACHE_DEFAULT_TIMEOUT": 300,
}
FILTER_STATE_CACHE_CONFIG = {
    "CACHE_TYPE": "SimpleCache",
    "CACHE_DEFAULT_TIMEOUT": 300,
}
EXPLORE_FORM_DATA_CACHE_CONFIG = {
    "CACHE_TYPE": "SimpleCache",
    "CACHE_DEFAULT_TIMEOUT": 300,
}

# Disable Celery (not needed for home use)
class CeleryConfig:
    broker_url = None
    result_backend = None

CELERY_CONFIG = CeleryConfig

# Public role for easy access via ingress
PUBLIC_ROLE_LIKE = "Gamma"

# Allow all origins (behind ingress)
CORS_OPTIONS = {
    "supports_credentials": True,
    "allow_headers": ["*"],
    "resources": ["*"],
    "origins": ["*"],
}

# Logging
LOG_LEVEL = "INFO"
EOF

echo "Superset configuration generated"

# Initialize Superset if first run
if [ ! -f "${INIT_FLAG}" ]; then
    echo "First run detected, initializing Superset..."

    # Initialize database
    echo "Running database migrations..."
    superset db upgrade

    # Create admin user
    echo "Creating admin user..."
    superset fab create-admin \
        --username admin \
        --firstname Admin \
        --lastname User \
        --email admin@haas.local \
        --password "${ADMIN_PASSWORD}" || true

    # Register Home Assistant database connection
    echo "Registering Home Assistant database connection..."
    python3 << 'PYTHON'
import os

from superset.app import create_app
app = create_app()

with app.app_context():
    from superset.extensions import db, security_manager
    from superset.models.core import Database

    # Get admin user for ownership
    admin_user = security_manager.find_user(username="admin")

    # Check if database already exists
    existing = db.session.query(Database).filter_by(database_name="Home Assistant").first()
    if not existing:
        ha_db = Database(
            database_name="Home Assistant",
            sqlalchemy_uri=os.environ.get("HA_DATABASE_URI"),
            expose_in_sqllab=True,
            allow_run_async=False,
            allow_ctas=False,
            allow_cvas=False,
            allow_dml=False,
        )
        if admin_user:
            ha_db.created_by_fk = admin_user.id
            ha_db.changed_by_fk = admin_user.id
        db.session.add(ha_db)
        db.session.commit()
        print("Home Assistant database registered successfully")
    else:
        print("Home Assistant database already registered")
PYTHON

    # Initialize roles and permissions AFTER database registration
    # This ensures the database permissions are synced correctly
    echo "Initializing roles and permissions..."
    superset init

    # Mark as initialized
    touch "${INIT_FLAG}"
    echo "Superset initialization complete"
else
    echo "Superset already initialized, running migrations..."
    superset db upgrade
fi

# Import default dashboards if not already done
if [ ! -f "${DASHBOARDS_FLAG}" ] && [ -f "/etc/superset/dashboards/ha_defaults.zip" ]; then
    echo "Importing default dashboards..."
    superset import-dashboards -p /etc/superset/dashboards/ha_defaults.zip || true
    touch "${DASHBOARDS_FLAG}"
    echo "Default dashboards imported"
fi

echo "================================================"
echo "Starting Superset web server..."
echo "Access via Home Assistant ingress or port 8088"
echo "================================================"

# Start Superset with gunicorn using HA ingress wrapper
exec gunicorn \
    --bind "0.0.0.0:8088" \
    --workers 2 \
    --timeout 120 \
    --limit-request-line 0 \
    --limit-request-field_size 0 \
    "ha_wsgi:application"
