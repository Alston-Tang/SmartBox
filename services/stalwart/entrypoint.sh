#!/bin/sh
set -eu

admin_user="${STALWART_ADMIN_USER:-admin}"
admin_password="${STALWART_ADMIN_PASSWORD:-smartbox-admin}"
recovery_admin="${admin_user}:${admin_password}"
config_path="/etc/stalwart/config.json"

# Keep STALWART_RECOVERY_ADMIN set throughout so the bootstrap script (and an
# operator) can authenticate against the JMAP API, including the running
# normally-started server, for provisioning and diagnostics.
export STALWART_RECOVERY_ADMIN="$recovery_admin"

run_bootstrap_phase() {
  # $1 = SMARTBOX_PHASE, remaining args = extra env exports already set by caller
  phase_status=1
  for _ in 1 2 3; do
    if SMARTBOX_PHASE="$1" python3 /opt/smartbox/bootstrap.py; then
      phase_status=0
      break
    fi
    sleep 2
  done
  return "$phase_status"
}

# Phase 1 (recovery mode): the recovery JMAP endpoint on :8080 lets us create
# the domain, plaintext listeners and mailbox account on a fresh database.
if [ ! -f /var/lib/stalwart/.smartbox-initialized-v5 ]; then
  export STALWART_RECOVERY_MODE=1
  /usr/local/bin/stalwart --config "$config_path" &
  stalwart_pid=$!

  run_bootstrap_phase recovery
  bootstrap_status=$?

  kill "$stalwart_pid" 2>/dev/null || true
  wait "$stalwart_pid" 2>/dev/null || true
  unset STALWART_RECOVERY_MODE

  if [ "$bootstrap_status" -ne 0 ]; then
    exit "$bootstrap_status"
  fi
fi

# Phase 2 (normal boot): the built-in roles and MTA objects are only seeded on a
# normal boot. This phase wires the Authentication singleton's default roles
# (without which the built-in User role grants no permissions and the server
# closes IMAP/SMTP sessions right after login as "socket error: EOF") and
# configures the outbound SMTP relay/smarthost when SMTP_RELAY_HOST is set.
if [ ! -f /var/lib/stalwart/.smartbox-roles-v1 ] || [ ! -f /var/lib/stalwart/.smartbox-relay-v1 ]; then
  /usr/local/bin/stalwart --config "$config_path" &
  stalwart_pid=$!

  run_bootstrap_phase roles
  roles_status=$?

  kill "$stalwart_pid" 2>/dev/null || true
  wait "$stalwart_pid" 2>/dev/null || true

  if [ "$roles_status" -ne 0 ]; then
    exit "$roles_status"
  fi
fi

exec /usr/local/bin/stalwart --config "$config_path"
