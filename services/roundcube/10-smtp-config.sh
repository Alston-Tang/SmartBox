#!/bin/sh
# Generate Roundcube SMTP settings from container env at startup.
#
# The official image only includes /var/roundcube/config/custom.inc.php, so this
# file is written outside that directory and pulled in by custom.inc.php. Values
# are baked in literally here (shell env is reliable at entrypoint time, unlike
# PHP getenv() under Apache at request time).
set -eu

out="/var/roundcube/smtp.generated.inc.php"

domain="${MAIL_DOMAIN:-}"
host="${MAIL_HOSTNAME:-$domain}"
smtp_user="%u"
if [ -n "$domain" ]; then
  smtp_user="%u@${domain}"
fi

{
  echo "<?php"
  echo "// Generated at container start by SmartBox (do not edit)."
  echo "\$config['smtp_user'] = '${smtp_user}';"
  echo "\$config['smtp_pass'] = '%p';"
  if [ -n "$host" ]; then
    echo "\$config['smtp_helo_host'] = '${host}';"
  fi
} > "$out"
