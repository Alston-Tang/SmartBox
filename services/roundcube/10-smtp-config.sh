#!/bin/sh
# Bake SMTP settings from container env (PHP getenv() is unreliable under Apache).
set -eu

domain="${MAIL_DOMAIN:-}"
host="${MAIL_HOSTNAME:-$domain}"
smtp_user="%u"
if [ -n "$domain" ]; then
  smtp_user="%u@${domain}"
fi

cat > /var/roundcube/config/smtp.inc.php <<EOF
<?php
// Generated at container start by SmartBox (do not edit).
\$config['smtp_user'] = '${smtp_user}';
\$config['smtp_pass'] = '%p';
EOF

if [ -n "$host" ]; then
  cat >> /var/roundcube/config/smtp.inc.php <<EOF
\$config['smtp_helo_host'] = '${host}';
EOF
fi
