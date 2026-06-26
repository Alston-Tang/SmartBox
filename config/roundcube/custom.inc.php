<?php
/**
 * SmartBox Roundcube overrides.
 *
 * Stalwart is plaintext-only on the internal Docker network (IMAP 143, SMTP 25).
 * SMTP AUTH is required to send mail and relay to external domains.
 *
 * With ROUNDCUBEMAIL_USERNAME_DOMAIN set, Roundcube stores only the local part
 * in the session (%u). Stalwart expects the full mailbox address for SMTP AUTH.
 */

$smtp_domain = getenv('ROUNDCUBEMAIL_USERNAME_DOMAIN') ?: '';
$config['smtp_user'] = $smtp_domain !== '' ? '%u@' . $smtp_domain : '%u';
$config['smtp_pass'] = '%p';

// Stalwart rejects non-FQDN EHLO hostnames on port 25 (e.g. container name "roundcube").
$helo_host = getenv('MAIL_HOSTNAME') ?: $smtp_domain;
if ($helo_host !== '') {
    $config['smtp_helo_host'] = $helo_host;
}

$config['product_name'] = 'SmartBox Webmail';
