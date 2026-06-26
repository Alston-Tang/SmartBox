<?php
/**
 * SmartBox Roundcube overrides (loaded by the official image from
 * /var/roundcube/config/custom.inc.php, after config.docker.inc.php).
 *
 * SMTP auth uses the logged-in user's credentials. %u is already the full
 * mailbox address because username_domain (set from MAIL_DOMAIN) appends the
 * domain at login, so it is used as-is for SMTP AUTH.
 */

$config['smtp_user'] = '%u';
$config['smtp_pass'] = '%p';

// Stalwart rejects non-FQDN EHLO hostnames on port 25. The container's HTTP host
// (e.g. "r730xd") is not an FQDN, so send the mail domain instead. username_domain
// is already set by config.docker.inc.php (from MAIL_DOMAIN) before this include.
if (!empty($config['username_domain']) && is_string($config['username_domain'])) {
    $config['smtp_helo_host'] = $config['username_domain'];
}

$config['product_name'] = 'SmartBox Webmail';
